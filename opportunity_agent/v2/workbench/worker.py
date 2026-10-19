"""Durable imports/indexing/reminders. Database leases survive process restarts."""
import asyncio
from contextlib import suppress
from datetime import timedelta, timezone
from email.message import EmailMessage
import logging
import os
import smtplib
from sqlalchemy import select, update, or_
from ..db.base import utcnow
from ..db.models import User
from ..core.telemetry import span
from .models import ImportJob, Project, Reminder, Notification, CalendarEvent
from .importer import process_job
from .service import rebuild_index

log = logging.getLogger(__name__)


def send_mail(to, title, body):
    message = EmailMessage()
    message['From'],message['To'],message['Subject'] = os.environ['SMTP_FROM'],to,title
    message.set_content(body)
    ssl = os.getenv('SMTP_SSL','0') == '1'
    client = smtplib.SMTP_SSL if ssl else smtplib.SMTP
    with client(os.environ['SMTP_HOST'],int(os.getenv('SMTP_PORT','465' if ssl else '587')),timeout=20) as smtp:
        if not ssl:
            smtp.starttls()
        if os.getenv('SMTP_USERNAME'):
            smtp.login(os.environ['SMTP_USERNAME'],os.environ['SMTP_PASSWORD'])
        smtp.send_message(message)


async def deliver_reminder(factory, reminder_id, mailer=send_mail):
    async with factory() as session:
        r = await session.get(Reminder,reminder_id)
        e = await session.get(CalendarEvent,r.event_id)
        if r.status != 'sending':
            return
        if not e or e.cancelled or e.version != r.event_version:
            r.status='cancelled';await session.commit();return
        n = await session.scalar(select(Notification).where(Notification.reminder_id == r.id))
        if n is None:
            due=r.due_at.replace(tzinfo=timezone.utc) if r.due_at.tzinfo is None else r.due_at
            n=Notification(user_id=r.user_id,reminder_id=r.id,title=e.payload['title'],
                delayed=(utcnow()-due).total_seconds()>60,email_status='pending' if r.email else 'disabled')
            session.add(n)
        await session.commit()
        if not r.email:
            r.status='delivered'
        elif not (os.getenv('SMTP_HOST') and os.getenv('SMTP_FROM')):
            n.email_status,r.status='unavailable','delivered'
        else:
            user=await session.get(User,r.user_id)
            # Recheck cancellation immediately before network delivery.
            await session.refresh(r);await session.refresh(e)
            if r.status != 'sending' or e.cancelled or e.version != r.event_version:
                return
            try:
                with span('workspace.reminder.send', reminder_id=r.id):
                    await asyncio.to_thread(mailer,user.email,('延迟提醒：' if n.delayed else '申请提醒：')+n.title,
                        f"{n.title}\n{e.payload.get('date','')} {e.payload.get('time','')} {e.payload.get('timezone','')}\n{e.payload.get('notes','')}")
                n.email_status,r.status='sent','delivered'
            except Exception:
                r.attempts+=1
                n.email_status='failed' if r.attempts>=3 else 'retrying'
                r.status='failed' if r.attempts>=3 else 'pending'
                r.lease_until=utcnow()+timedelta(seconds=60 if r.attempts==1 else 300)
        if r.status != 'pending':
            r.lease_until=None
        await session.commit()


async def claim(session, cls, predicate, *, lease_seconds):
    now=utcnow()
    row=await session.scalar(select(cls).where(predicate,
        or_(cls.lease_until.is_(None),cls.lease_until<now)).order_by(cls.created_at).with_for_update(skip_locked=True).limit(1))
    if row is None:
        return None
    if cls is ImportJob and row.attempts>=3:
        row.status,row.error_code='failed','WORKER_RECOVERY_LIMIT'
        await session.commit()
        return None
    old_status=row.status
    values={'status':'sending' if cls is Reminder else 'reading','lease_until':now+timedelta(seconds=lease_seconds)}
    if cls is ImportJob:
        values['attempts']=ImportJob.attempts+1
    result=await session.execute(update(cls).where(cls.id==row.id,cls.status==old_status,
        or_(cls.lease_until.is_(None),cls.lease_until<now)).values(**values))
    await session.commit()
    return row.id if result.rowcount else None


async def tick(factory):
    async with factory() as session:
        reminder=await claim(session,Reminder,Reminder.status.in_(['pending','sending']) & (Reminder.due_at<=utcnow()),lease_seconds=60)
    if reminder:
        await deliver_reminder(factory,reminder)


async def import_loop(factory):
    # Two independent consumers enforce per-process import concurrency=2.
    while True:
        try:
            async with factory() as session:
                job=await claim(session,ImportJob,ImportJob.status.in_(['queued','reading','extracting']),lease_seconds=360)
            if job:
                await process_job(factory,job)
            else:
                await asyncio.sleep(2)
        except Exception:
            log.warning('Workspace import worker unavailable',exc_info=False)
            await asyncio.sleep(5)


async def index_loop(factory):
    while True:
        try:
            async with factory() as session:
                p=await session.scalar(select(Project).where(Project.index_status=='pending',Project.published_version>0)
                    .with_for_update(skip_locked=True).limit(1))
                if p:
                    with span('workspace.index',project_id=p.id):
                        await rebuild_index(session,p)
                    await session.commit()
            await asyncio.sleep(2)
        except Exception:
            log.warning('Workspace index worker unavailable',exc_info=False)
            await asyncio.sleep(5)


async def reminder_loop(factory):
    while True:
        try:
            # Drain due work without limiting throughput to one notification per 30 seconds.
            for _ in range(100):
                await tick(factory)
        except Exception:
            log.warning('Workspace reminder worker unavailable',exc_info=False)
        await asyncio.sleep(30)


async def run_workspace_worker(factory):
    async with asyncio.TaskGroup() as group:
        group.create_task(import_loop(factory));group.create_task(import_loop(factory))
        group.create_task(index_loop(factory));group.create_task(reminder_loop(factory))
