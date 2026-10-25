/* Pure projections shared by the UI and Node tests. */
(function(root){
  'use strict';
  const isOpen=t=>t.status==='planned'||t.status==='in_progress';
  const taskSort=(a,b)=>(a.due_date||'9999-12-31').localeCompare(b.due_date||'9999-12-31')||(a.created_at||'').localeCompare(b.created_at||'')||a.id.localeCompare(b.id);
  function directory(projects,tasks){
    const schools=new Map();
    for(const project of projects){const key=project.university.trim().replace(/\s+/g,' ').toLocaleLowerCase();
      if(!schools.has(key))schools.set(key,{key,name:project.university,projects:[],count:0});
      const school=schools.get(key),items=tasks.filter(t=>t.application_id===project.id&&isOpen(t));
      school.projects.push({...project,open_count:items.length});school.count+=items.length;
    }
    return {global:tasks.filter(t=>!t.application_id&&isOpen(t)).length,schools:[...schools.values()].sort((a,b)=>a.name.localeCompare(b.name))};
  }
  function visibleTasks(tasks,applicationId,completed=false){return tasks.filter(t=>(t.application_id||null)===(applicationId||null)&&(completed?t.status==='completed':isOpen(t))).sort(taskSort);}
  function localDate(value=new Date()){return `${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,'0')}-${String(value.getDate()).padStart(2,'0')}`;}
  const api={isOpen,taskSort,directory,visibleTasks,localDate};
  root.ApplyOpsState=api;
  if(typeof module!=='undefined')module.exports=api;
})(typeof window!=='undefined'?window:globalThis);
