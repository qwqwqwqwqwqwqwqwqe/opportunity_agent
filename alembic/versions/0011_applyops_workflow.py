"""Add ApplyOps workflow without changing historical revisions.

Frozen table definitions: do not import live ORM mappings here. The old 0001
uses live metadata, so existing tables are structurally checked before reuse.
"""
from alembic import op
import sqlalchemy as sa

revision = "0011_applyops_workflow"
down_revision = "0010_application_workspace"
branch_labels = None
depends_on = None


def c(name, typ, nullable=False, fk=None):
    return sa.Column(name, typ, *([sa.ForeignKey(fk)] if fk else []), nullable=nullable)


def table(name, *columns, versioned=False, constraints=()):
    columns = [c("id", sa.String()), c("created_at", sa.DateTime(timezone=True)),
               c("updated_at", sa.DateTime(timezone=True)), c("user_id", sa.String(), fk="users.id"), *columns]
    columns[0].primary_key = True
    if versioned:
        columns.append(c("version", sa.Integer()))
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(name):
        actual = {col["name"]: col for col in inspector.get_columns(name)}
        for col in columns:
            if col.name not in actual or actual[col.name]["nullable"] != col.nullable:
                raise RuntimeError(f"incompatible existing column: {name}.{col.name}")
            expected_type = col.type.compile(dialect=op.get_bind().dialect)
            actual_type = actual[col.name]["type"].compile(dialect=op.get_bind().dialect)
            # SQLAlchemy generic String compiles to VARCHAR just like mappings.
            if expected_type != actual_type:
                raise RuntimeError(f"incompatible type: {name}.{col.name}: {actual_type} != {expected_type}")
        uniques = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(name)}
        for constraint in constraints:
            if isinstance(constraint, sa.UniqueConstraint):
                names = tuple(constraint.columns.keys()) or tuple(constraint._pending_colargs)
                if names not in uniques:
                    raise RuntimeError(f"missing unique constraint: {name}.{names}")
        return
    op.create_table(name, *columns, *constraints)
    op.create_index(f"ix_{name}_user_id", name, ["user_id"])


def unique(name, *fields):
    return sa.UniqueConstraint(*fields, name=name)


def upgrade():
    S, I, J, D, T, B = sa.String, sa.Integer, sa.JSON, sa.Date, lambda: sa.DateTime(timezone=True), sa.Boolean
    table("workflow_programs", c("university", S(200)), c("name", S(200)), c("degree", S(80)),
          c("home_unit", S(200)), c("aliases", J()), c("archived_at", T(), True), versioned=True)
    table("workflow_offerings", c("program_id", S(), fk="workflow_programs.id"), c("cycle", S(40), True),
          c("deadline_date", D(), True), c("deadline_at", T(), True), c("timezone", S(80)),
          c("date_precision", S(16)), c("gre_policy", S(32)), c("requirements_json", J()), versioned=True,
          constraints=[unique("uq_workflow_offering", "user_id", "program_id", "cycle")])
    for name, column in [("applications", c("offering_id", S(), True, "workflow_offerings.id")),
                         ("applications", sa.Column("priority", I(), nullable=False, server_default="0")),
                         ("application_plans", sa.Column("schema_version", I(), nullable=False, server_default="2")),
                         ("application_plans", c("base_plan_id", S(64), True)),
                         ("application_plans", c("input_revision", I(), True))]:
        if column.name not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns(name)}:
            if name == "applications" and column.name == "offering_id" and op.get_bind().dialect.name == "sqlite":
                with op.batch_alter_table(name) as batch:
                    batch.add_column(sa.Column("offering_id", S(), nullable=True))
                    batch.create_foreign_key("fk_applications_workflow_offering", "workflow_offerings", ["offering_id"], ["id"])
            else:
                op.add_column(name, column)
    table("workflow_sources", c("kind", S(32)), c("source_ref", S(160), True), c("content_hash", S(64)), c("excerpt", sa.Text()))
    table("workflow_field_observations", c("target_type", S(32)), c("target_id", S(64)), c("field", S(80)),
          c("value", J()), c("source_id", S(), fk="workflow_sources.id"), c("verification_status", S(32)), c("decision_status", S(32)))
    table("workflow_requirements", c("application_id", S(), fk="applications.id"), c("key", S(160)), c("kind", S(40)),
          c("obligation", S(16)), c("required_count", I(), True), c("criteria", J()), c("source_id", S(), True, "workflow_sources.id"),
          versioned=True, constraints=[unique("uq_workflow_requirement", "application_id", "key"),
              sa.CheckConstraint("required_count IS NULL OR required_count > 0", name="ck_workflow_requirement_count")])
    table("workflow_materials", c("requirement_id", S(), fk="workflow_requirements.id"), c("slot_key", S(160)),
          c("status", S(24)), c("evidence_ref", S(), True, "workflow_sources.id"), versioned=True,
          constraints=[unique("uq_workflow_material", "requirement_id", "slot_key")])
    table("workflow_tasks", c("application_id", S(), True, "applications.id"), c("requirement_id", S(), True, "workflow_requirements.id"),
          c("scope_key", S(64)), c("stable_key", S(160)), c("title", S(300)), c("status", S(24)), c("start_date", D(), True),
          c("due_date", D(), True), c("date_certainty", S(16)), c("duration_days", I()), c("locked", B()), c("evidence", sa.Text()),
          c("legacy_task_id", S(64), True), versioned=True,
          constraints=[unique("uq_workflow_task", "user_id", "scope_key", "stable_key"), unique("uq_workflow_legacy_task", "legacy_task_id"),
                       sa.CheckConstraint("duration_days > 0", name="ck_workflow_task_duration")])
    table("workflow_task_dependencies", c("predecessor_id", S(), fk="workflow_tasks.id"), c("successor_id", S(), fk="workflow_tasks.id"),
          c("lag_days", I()), constraints=[unique("uq_workflow_dependency", "predecessor_id", "successor_id"),
                         sa.CheckConstraint("predecessor_id != successor_id", name="ck_workflow_dependency_self")])
    table("workflow_calendar_events", c("type", S(32)), c("title", S(300)), c("date", D(), True), c("start_at", T(), True),
          c("end_at", T(), True), c("timezone", S(80)), c("date_precision", S(16)), versioned=True)
    table("workflow_plan_items", c("plan_id", S(), fk="application_plans.id"), c("task_id", S(), True, "workflow_tasks.id"),
          c("proposal_key", S(200)), c("task_spec", J()), c("schedule_snapshot", J()), c("input_task_version", I(), True),
          constraints=[unique("uq_workflow_plan_item", "plan_id", "proposal_key")])
    table("workflow_state", c("planning_revision", I()), c("next_plan_version", I()), c("availability", J()),
          constraints=[unique("uq_workflow_state_user", "user_id")])
    table("workflow_command_receipts", c("operation", S(64)), c("request_id", S(160)), c("payload_hash", S(64)), c("response", J()),
          constraints=[unique("uq_workflow_receipt", "user_id", "operation", "request_id")])
    table("workflow_domain_events", c("aggregate_type", S(64)), c("aggregate_id", S(64)), c("aggregate_version", I()),
          c("type", S(80)), c("payload", J()), c("delivery_status", S(24)))
    table("workflow_jobs", c("dedupe_key", S(200)), c("type", S(64)), c("scope", J()), c("input_revision", I()), c("status", S(24)),
          c("attempt", I()), c("lease_token", S(64), True), c("lease_until", T(), True), c("error", sa.Text(), True), c("result", J()),
          constraints=[unique("uq_workflow_job_dedupe", "dedupe_key")])
    table("workflow_notifications", c("type", S(64)), c("target_id", S(64)), c("message", sa.Text()), c("dedupe_key", S(200)),
          c("read_at", T(), True), constraints=[unique("uq_workflow_notification_dedupe", "dedupe_key")])
    index_names = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("application_plans")}
    if "uq_workflow_active_plan" not in index_names:
        op.create_index("uq_workflow_active_plan", "application_plans", ["user_id"], unique=True,
                        postgresql_where=sa.text("status = 'active' AND schema_version = 3"),
                        sqlite_where=sa.text("status = 'active' AND schema_version = 3"))
    for name, column in [("workflow_domain_events", "delivery_status"), ("workflow_jobs", "status")]:
        index_name = f"ix_{name}_{column}"
        if index_name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(name)}:
            op.create_index(index_name, name, [column])


def downgrade():
    raise RuntimeError("ApplyOps contains user data; restore a backup instead of destructive downgrade")
