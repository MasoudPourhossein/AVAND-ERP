(function(){
  if(!window.Chart)return;
  Chart.defaults.font.family='Vazirmatn, Tahoma, sans-serif';
  Chart.defaults.color='#64748b';
  const P=['#4f46e5','#94a3b8','#0891b2','#f59e0b','#10b981','#ef4444'];
  const fa=n=>Number(n||0).toLocaleString('en-US');
  window.mkChart=function(id,c){
    const el=document.getElementById(id); if(!el)return;
    const hz=!!c.horizontal;
    new Chart(el,{type:c.type,data:{labels:c.labels,datasets:c.sets.map((s,i)=>({label:s[0],data:s[1],
      backgroundColor:c.type==='line'?P[i]+'22':P[i],borderColor:P[i],borderWidth:c.type==='line'?2.5:0,borderRadius:6,
      tension:.35,fill:c.type==='line'&&i===0,pointRadius:c.type==='line'?3:0,spanGaps:false,maxBarThickness:36}))},
      options:{indexAxis:hz?'y':'x',maintainAspectRatio:false,responsive:true,
        plugins:{legend:{display:c.sets.length>1,position:'bottom',rtl:true},tooltip:{rtl:true,callbacks:{label:x=>' '+x.dataset.label+': '+fa(x.raw)}}},
        scales:{x:{reverse:true,grid:{display:hz},ticks:{callback:function(v){return hz?fa(v):this.getLabelForValue(v)}}},
                y:{position:hz?'right':'right',grid:{color:'#eef2f7',display:!hz},ticks:{callback:function(v){return hz?this.getLabelForValue(v):fa(v)}}}}}});
  };
})();
