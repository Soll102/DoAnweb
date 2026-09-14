// AZUREA — Chatbot + hiệu ứng chuyển động
(function(){
  // Navbar đổ bóng khi cuộn
  const nav = document.getElementById('navbar');
  addEventListener('scroll', ()=> nav && nav.classList.toggle('scrolled', scrollY > 10), {passive:true});

  // Reveal on scroll
  const io = new IntersectionObserver(entries=>{
    entries.forEach(e=>{ if(e.isIntersecting){ e.target.classList.add('visible'); io.unobserve(e.target); } });
  },{threshold:.12});
  document.querySelectorAll('.reveal,.reveal-left,.reveal-right').forEach(el=>io.observe(el));

  // Đếm số liệu stats
  const cio = new IntersectionObserver(entries=>{
    entries.forEach(e=>{
      if(!e.isIntersecting) return;
      const el = e.target, end = parseFloat((el.dataset.count||'').replace(/,/g,'')) || 0;
      if(!end) return;
      cio.unobserve(el);
      const t0 = performance.now(), dur = 1400;
      (function tick(t){
        const p = Math.min(1,(t-t0)/dur), ease = 1-Math.pow(1-p,3);
        el.textContent = Math.round(end*ease).toLocaleString('vi-VN') + (end>1000?'+':'');
        if(p<1) requestAnimationFrame(tick);
      })(t0);
    });
  },{threshold:.5});
  document.querySelectorAll('[data-count]').forEach(el=>cio.observe(el));

  // Hero slider tự chạy + dots
  const slides = [...document.querySelectorAll('#hero-slides img')];
  const dotsBox = document.getElementById('hero-dots');
  if(slides.length && dotsBox){
    slides.forEach((_,i)=>{
      const d = document.createElement('button');
      d.setAttribute('aria-label','Ảnh '+(i+1));
      if(i===0) d.classList.add('active');
      d.onclick = ()=>go(i, true);
      dotsBox.appendChild(d);
    });
    const dots = [...dotsBox.children];
    let cur = 0, timer;
    function go(i, manual){
      slides[cur].classList.remove('active'); dots[cur].classList.remove('active');
      cur = (i+slides.length)%slides.length;
      // restart kenburns
      const s = slides[cur]; s.style.animation='none'; void s.offsetWidth; s.style.animation='';
      s.classList.add('active'); dots[cur].classList.add('active');
      if(manual) restart();
    }
    function restart(){ clearInterval(timer); timer = setInterval(()=>go(cur+1), 6000); }
    restart();
  }

  // Lightbox cho mọi ảnh gallery
  const lb = document.getElementById('lightbox'), lbImg = document.getElementById('lightbox-img');
  document.querySelectorAll('.g-item img, .marquee-track img, .footer-gallery img, .feature-photo img').forEach(img=>{
    img.addEventListener('click', ()=>{ if(!lb) return; lbImg.src = img.src.replace('w=600','w=1400').replace('w=400','w=1400').replace('w=800','w=1400'); lb.classList.add('open'); });
  });
  lb && lb.addEventListener('click', ()=>lb.classList.remove('open'));
  addEventListener('keydown', e=>{ if(e.key==='Escape') lb && lb.classList.remove('open'); });

  // ---- Chatbot Marina ----
  const fab = document.getElementById('chat-fab');
  const box = document.getElementById('chat-box');
  const msgs = document.getElementById('chat-msgs');
  const form = document.getElementById('chat-form');
  const input = document.getElementById('chat-input');
  if(!fab) return;
  const SID = 'web-' + Math.random().toString(36).slice(2,9);
  let greeted = false;
  fab.onclick = () => {
    box.classList.toggle('open');
    if(!greeted && box.classList.contains('open')){
      greeted = true;
      pushBot("Xin chào! Tôi là <b>Marina</b> — trợ lý của AZUREA ISLANDS.<br>Tôi có thể <b>đặt phòng hộ bạn</b>, <b>gọi nhân viên tư vấn</b>, tra <b>giá phòng</b> và <b>lịch trống</b>.<br>Bạn cần gì hôm nay?",
        ["Đặt hộ tôi","Gặp nhân viên","Xem bảng giá","Lịch trống"]);
    }
  };
  function scroll(){ msgs.scrollTop = msgs.scrollHeight; }
  function bubble(html, who){
    const d = document.createElement('div');
    d.className = 'msg ' + who; d.innerHTML = html;
    msgs.appendChild(d); scroll(); return d;
  }
  function pushBot(html, suggestions){
    const d = bubble(html, 'bot');
    if(suggestions && suggestions.length){
      const row = document.createElement('div');
      row.className = 'chip-row';
      suggestions.forEach(s=>{
        const c = document.createElement('button');
        c.className='chip'; c.type='button'; c.textContent=s;
        c.onclick=()=>{ input.value=s; form.requestSubmit(); };
        row.appendChild(c);
      });
      d.appendChild(row); scroll();
    }
  }
  form.addEventListener('submit', async (e)=>{
    e.preventDefault();
    const text = input.value.trim();
    if(!text) return;
    bubble(text.replace(/</g,'&lt;'), 'user');
    input.value='';
    const typing = bubble('Marina đang trả lời...','bot');
    try{
      const res = await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({message:text, session_id:SID})});
      const data = await res.json();
      typing.remove();
      pushBot(data.reply, data.suggestions||[]);
    }catch(err){ typing.innerHTML='Mất kết nối, bạn gọi hotline 1900 6868 giúp Marina nhé!'; }
  });
})();

// Kiểm tra nhanh lịch trống ở form đặt phòng
async function quickCheck(roomId){
  const ci = document.getElementById('check_in')?.value;
  const co = document.getElementById('check_out')?.value;
  const out = document.getElementById('check-result');
  if(!ci || !co || !out) return;
  out.innerHTML = 'Đang kiểm tra...';
  const fd = new FormData(); fd.append('room_id', roomId); fd.append('check_in', ci); fd.append('check_out', co);
  try{
    const res = await fetch('/api/check',{method:'POST', body:fd});
    const d = await res.json();
    out.innerHTML = d.ok
      ? `<b>Còn trống:</b> ${d.nights} đêm — Tổng: <b>${d.total_text}</b>`
      : `${d.message}`;
    out.className = 'alert ' + (d.ok ? 'alert-success' : 'alert-danger');
  }catch(e){ out.innerHTML = 'Không kiểm tra được, vui lòng thử lại.'; out.className='alert alert-warning'; }
}
