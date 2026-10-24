// Offline WebGL STL viewer with point picking and measuring. No CDN, build step or third-party packages.
const SVG = 'http://www.w3.org/2000/svg';
export class Viewer {
  constructor(canvas){
    this.canvas=canvas;this.yaw=-.72;this.pitch=.62;this.zoom=1;this.pan=[0,0];this.count=0;this.radius=1;this.center=[0,0,0];
    this.tris=null;this.norms=null;this.stats=null;this.marks=[];this.lines=[];this.onPick=null;
    const gl=this.gl=canvas.getContext('webgl',{antialias:true,alpha:true,preserveDrawingBuffer:true});
    if(!gl)throw Error('This browser does not support WebGL. Open the app in Chrome or Edge.');
    const shader=(type,source)=>{let s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));return s;};
    const vs=shader(gl.VERTEX_SHADER,`attribute vec3 position;attribute vec3 normal;uniform mat4 mvp;uniform mat4 rotation;varying vec3 n;void main(){gl_Position=mvp*vec4(position,1.0);n=mat3(rotation)*normal;}`);
    const fs=shader(gl.FRAGMENT_SHADER,`precision mediump float;varying vec3 n;void main(){vec3 nn=normalize(n);float light=.42+.45*abs(dot(nn,normalize(vec3(-.4,.7,1.0))))+.13*abs(dot(nn,normalize(vec3(.8,.1,.4))));gl_FragColor=vec4(vec3(.39,.65,.70)*light,1.0);}`);
    this.program=gl.createProgram();gl.attachShader(this.program,vs);gl.attachShader(this.program,fs);gl.linkProgram(this.program);if(!gl.getProgramParameter(this.program,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(this.program));
    this.pos=gl.getAttribLocation(this.program,'position');this.norm=gl.getAttribLocation(this.program,'normal');this.mvp=gl.getUniformLocation(this.program,'mvp');this.rot=gl.getUniformLocation(this.program,'rotation');this.buffer=gl.createBuffer();
    // Marks and measurements are drawn in an SVG layer on top: dark lines with a white halo and written labels.
    this.svg=document.createElementNS(SVG,'svg');this.svg.setAttribute('class','viewer-overlay');this.svg.setAttribute('aria-hidden','true');canvas.after(this.svg);
    this.drag=null;
    canvas.addEventListener('pointerdown',e=>{this.drag={x:e.clientX,y:e.clientY,x0:e.clientX,y0:e.clientY,t:Date.now(),pan:e.shiftKey||e.button===2,moved:false};try{canvas.setPointerCapture(e.pointerId);}catch(_){}});
    canvas.addEventListener('pointermove',e=>{if(!this.drag)return;const dx=e.clientX-this.drag.x,dy=e.clientY-this.drag.y;this.drag.x=e.clientX;this.drag.y=e.clientY;if(Math.hypot(e.clientX-this.drag.x0,e.clientY-this.drag.y0)>5)this.drag.moved=true;if(!this.drag.moved)return;if(this.drag.pan){this.pan[0]+=dx/canvas.clientWidth*2;this.pan[1]-=dy/canvas.clientHeight*2;}else{this.yaw+=dx*.008;this.pitch=Math.max(-1.56,Math.min(1.56,this.pitch+dy*.008));}this.draw();});
    canvas.addEventListener('pointerup',e=>{const d=this.drag;this.drag=null;if(d&&!d.moved&&!d.pan&&e.button===0&&Date.now()-d.t<600&&this.onPick){const r=canvas.getBoundingClientRect();const hit=this.pick(e.clientX-r.left,e.clientY-r.top);if(hit)this.onPick(hit);}});
    canvas.addEventListener('pointercancel',()=>this.drag=null);canvas.addEventListener('contextmenu',e=>e.preventDefault());canvas.addEventListener('wheel',e=>{e.preventDefault();this.zoom=Math.max(.2,Math.min(8,this.zoom*Math.exp(-e.deltaY*.001)));this.draw();},{passive:false});
    new ResizeObserver(()=>this.draw()).observe(canvas);this.draw();
  }
  async load(url){const response=await fetch(url);if(!response.ok)throw Error('Could not open the STL preview');const raw=await response.arrayBuffer();this.loadBuffer(raw);}
  loadBuffer(raw){
    const data=new DataView(raw),bounds=[[Infinity,Infinity,Infinity],[-Infinity,-Infinity,-Infinity]];let count=raw.byteLength>=84?data.getUint32(80,true):0;let tris=[];
    if(count>0&&84+count*50===raw.byteLength){if(count>1000000)throw Error('Preview is too large');tris=new Float32Array(count*9);for(let i=0;i<count;i++){const offset=84+i*50+12;for(let k=0;k<9;k++)tris[i*9+k]=data.getFloat32(offset+k*4,true);}}
    else{const text=new TextDecoder().decode(raw);const matches=[...text.matchAll(/vertex\s+([-+\deE.]+)\s+([-+\deE.]+)\s+([-+\deE.]+)/g)];if(matches.length>3000000)throw Error('Preview is too large');count=Math.floor(matches.length/3);tris=new Float32Array(count*9);for(let i=0;i<count*3;i++)for(let k=0;k<3;k++)tris[i*3+k]=Number(matches[i][k+1]);}
    if(!count)throw Error('No triangles in STL');
    const values=new Float32Array(count*18),norms=new Float32Array(count*3);let area=0,volume=0;
    for(let i=0;i<count;i++){const t=i*9,a=[tris[t],tris[t+1],tris[t+2]],b=[tris[t+3],tris[t+4],tris[t+5]],c=[tris[t+6],tris[t+7],tris[t+8]];
      for(const p of [a,b,c]){if(!p.every(Number.isFinite))throw Error('Invalid STL coordinates');for(let k=0;k<3;k++){bounds[0][k]=Math.min(bounds[0][k],p[k]);bounds[1][k]=Math.max(bounds[1][k],p[k]);}}
      const u=b.map((v,k)=>v-a[k]),v=c.map((v,k)=>v-a[k]);let n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];const len=Math.hypot(...n);area+=len/2;volume+=(a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0]))/6;n=n.map(x=>x/(len||1));
      norms.set(n,i*3);[a,b,c].forEach((p,j)=>{values.set(p,i*18+j*6);values.set(n,i*18+j*6+3);});}
    this.tris=tris;this.norms=norms;this.stats={area,volume:Math.abs(volume),triangles:count};
    this.center=bounds[0].map((x,i)=>(x+bounds[1][i])/2);this.radius=Math.hypot(...bounds[0].map((x,i)=>bounds[1][i]-x))/2||1;
    this.count=count*3;this.gl.bindBuffer(this.gl.ARRAY_BUFFER,this.buffer);this.gl.bufferData(this.gl.ARRAY_BUFFER,values,this.gl.STATIC_DRAW);this.marks=[];this.lines=[];this.fit();
  }
  clear(){this.count=0;this.tris=null;this.stats=null;this.marks=[];this.lines=[];this.draw();}
  fit(){this.zoom=1;this.pan=[0,0];this.draw();}
  view(view){if(view==='top'){this.pitch=1.56;this.yaw=0;}else if(view==='front'){this.pitch=0;this.yaw=0;}else{this.pitch=.62;this.yaw=-.72;}this.draw();}
  setMarks(marks,lines){this.marks=marks||[];this.lines=lines||[];this.draw();}
  frame(){
    const w=Math.max(1,this.canvas.clientWidth),h=Math.max(1,this.canvas.clientHeight);
    const cy=Math.cos(this.yaw),sy=Math.sin(this.yaw),cp=Math.cos(this.pitch),sp=Math.sin(this.pitch);
    // Z-up model, screen right and up, with camera orbit around the origin; +depth points toward the camera.
    const right=[cy,sy,0],up=[-sy*sp,cy*sp,cp],depth=[sy*cp,-cy*cp,sp];
    const aspect=w/h,s=.8*this.zoom/this.radius;
    return {w,h,right,up,depth,sx:s/Math.max(1,aspect),sY:s*Math.min(1,aspect),sz:-.2/this.radius,c:this.center};
  }
  project(p,f=this.frame()){const d=p.map((v,i)=>v-f.c[i]);const dot=(a)=>a[0]*d[0]+a[1]*d[1]+a[2]*d[2];return [((f.sx*dot(f.right)+this.pan[0])+1)/2*f.w,(1-(f.sY*dot(f.up)+this.pan[1]))/2*f.h];}
  pick(x,y){
    if(!this.tris)return null;const f=this.frame(),t=this.tris,n=t.length/9;
    const u0=((2*x/f.w-1)-this.pan[0])/f.sx,v0=((1-2*y/f.h)-this.pan[1])/f.sY;let best=null,bestW=-Infinity;
    const R=f.right,U=f.up,D=f.depth,c=f.c;
    for(let i=0;i<n;i++){const o=i*9;let us=[0,0,0],vs=[0,0,0],ws=[0,0,0];
      for(let j=0;j<3;j++){const px=t[o+j*3]-c[0],py=t[o+j*3+1]-c[1],pz=t[o+j*3+2]-c[2];us[j]=R[0]*px+R[1]*py+R[2]*pz;vs[j]=U[0]*px+U[1]*py+U[2]*pz;ws[j]=D[0]*px+D[1]*py+D[2]*pz;}
      const den=(vs[1]-vs[2])*(us[0]-us[2])+(us[2]-us[1])*(vs[0]-vs[2]);if(Math.abs(den)<1e-12)continue;
      const a=((vs[1]-vs[2])*(u0-us[2])+(us[2]-us[1])*(v0-vs[2]))/den,b=((vs[2]-vs[0])*(u0-us[2])+(us[0]-us[2])*(v0-vs[2]))/den,g=1-a-b;
      if(a<-1e-6||b<-1e-6||g<-1e-6)continue;const w=a*ws[0]+b*ws[1]+g*ws[2];if(w>bestW){bestW=w;best=[i,a,b,g];}}
    if(!best)return null;const [i,a,b,g]=best,o=i*9;
    const point=[0,1,2].map(k=>a*t[o+k]+b*t[o+3+k]+g*t[o+6+k]);return {point,normal:Array.from(this.norms.slice(i*3,i*3+3))};
  }
  overlay(f){
    const svg=this.svg;svg.replaceChildren();if(!this.count||(!this.marks.length&&!this.lines.length))return;svg.setAttribute('viewBox',`0 0 ${f.w} ${f.h}`);
    const el=(tag,attrs,text)=>{const e=document.createElementNS(SVG,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;svg.append(e);return e;};
    const label=(x,y,text)=>{const t=el('text',{x:x+9,y:y-9,class:'viewer-label'},text);const b=t.getBBox();el('rect',{x:b.x-4,y:b.y-2,width:b.width+8,height:b.height+4,rx:3,class:'viewer-label-bg'});svg.append(t);};
    for(const [a,b,text] of this.lines){const p=this.project(a,f),q=this.project(b,f);el('line',{x1:p[0],y1:p[1],x2:q[0],y2:q[1],class:'viewer-halo'});el('line',{x1:p[0],y1:p[1],x2:q[0],y2:q[1],class:'viewer-line'});if(text)label((p[0]+q[0])/2,(p[1]+q[1])/2,text);}
    for(const m of this.marks){const p=this.project(m.point,f);el('circle',{cx:p[0],cy:p[1],r:7,class:'viewer-dot'});el('text',{x:p[0],y:p[1]+4,class:'viewer-dot-text'},m.label);}
  }
  draw(){
    const gl=this.gl;if(!gl)return;const ratio=Math.min(window.devicePixelRatio||1,2);let w=Math.max(1,Math.round(this.canvas.clientWidth*ratio)),h=Math.max(1,Math.round(this.canvas.clientHeight*ratio));if(this.canvas.width!==w||this.canvas.height!==h){this.canvas.width=w;this.canvas.height=h;}gl.viewport(0,0,w,h);gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
    const f=this.frame();this.overlay(f);if(!this.count)return;
    const {right,up,depth,sx,sY,sz,c}=f;
    const rot=new Float32Array([right[0],up[0],depth[0],0,right[1],up[1],depth[1],0,right[2],up[2],depth[2],0,0,0,0,1]);
    const m=new Float32Array([sx*right[0],sY*up[0],sz*depth[0],0,sx*right[1],sY*up[1],sz*depth[1],0,sx*right[2],sY*up[2],sz*depth[2],0,-sx*right.reduce((a,v,i)=>a+v*c[i],0)+this.pan[0],-sY*up.reduce((a,v,i)=>a+v*c[i],0)+this.pan[1],-sz*depth.reduce((a,v,i)=>a+v*c[i],0),1]);
    gl.enable(gl.DEPTH_TEST);gl.disable(gl.CULL_FACE);gl.useProgram(this.program);gl.bindBuffer(gl.ARRAY_BUFFER,this.buffer);gl.enableVertexAttribArray(this.pos);gl.vertexAttribPointer(this.pos,3,gl.FLOAT,false,24,0);gl.enableVertexAttribArray(this.norm);gl.vertexAttribPointer(this.norm,3,gl.FLOAT,false,24,12);gl.uniformMatrix4fv(this.mvp,false,m);gl.uniformMatrix4fv(this.rot,false,rot);gl.drawArrays(gl.TRIANGLES,0,this.count);
  }
}
