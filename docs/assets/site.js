const b=document.querySelector('.menu-btn'),n=document.querySelector('.nav');
if(b&&n){b.addEventListener('click',()=>{const o=n.classList.toggle('open');b.setAttribute('aria-expanded',o)})}
