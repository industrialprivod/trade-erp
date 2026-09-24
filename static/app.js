function openModal(id){document.getElementById(id)?.classList.add('open')}
function closeModal(id){document.getElementById(id)?.classList.remove('open')}
document.addEventListener('click',e=>{if(e.target.classList.contains('modal'))e.target.classList.remove('open')})
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelectorAll('.modal.open').forEach(x=>x.classList.remove('open'))})
