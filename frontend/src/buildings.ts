// Building layer data: the published files of buildings/by_area, read straight from GitHub in the browser.
// Row encoding: buildings/compact_layer.py (SCHEMA, decode). No backend involvement.
import {parquetRead} from 'hyparquet';
import {decompressZstd} from 'hyparquet-compressors';

export const BUILDINGS_BASE='https://raw.githubusercontent.com/mrdo19111911/hanoi-traffic-model/main/buildings/by_area/';
// Zoom 16 keeps a view to a few files; at 15 a view in central Hanoi already needs 300k-building files (ha_noi_04).
export const MIN_BUILDING_ZOOM=16;
// At most this many polygons, the ones nearest the view centre, from the view plus VIEW_MARGIN of its size per side.
export const MAX_DRAWN_BUILDINGS=15000,VIEW_MARGIN=.2;
export const MEMORY_LIMIT=150e6,CACHE_NAME='buildings-v1';
const GRID_DEG=.002;                       // spatial grid cell, about 200 m

export type BBox=[number,number,number,number];              // [west, south, east, north]
export type Ring=[number,number][];                            // [lon, lat], not closed
export interface Footprint{outer:Ring;holes:Ring[]}
export interface IndexEntry{file:string;province?:string;province_iso?:string;buildings:number;bytes:number;bbox:BBox}
export interface BuildingIndex{files:IndexEntry[];offline?:boolean}

export const buildingsShown=(zoom:number|null|undefined)=>zoom!=null&&zoom>=MIN_BUILDING_ZOOM;
export const intersects=(a:BBox,b:BBox)=>a[0]<=b[2]&&b[0]<=a[2]&&a[1]<=b[3]&&b[1]<=a[3];
export function padBBox([w,s,e,n]:BBox,f:number):BBox{const dx=(e-w)*f,dy=(n-s)*f;return[w-dx,s-dy,e+dx,n+dy];}
// Files whose bounding box meets the view. Bounding boxes of commune groups overlap, so this can pick a file that has
// no building in the view; it is still downloaded once and cached.
export const filesForBBox=(files:IndexEntry[],bbox:BBox)=>files.filter(f=>Array.isArray(f.bbox)&&f.bbox.length===4&&intersects(f.bbox,bbox));
// Share of a file's bounding box inside the view: tight files that sit in the view first, loose wide ones last.
export function overlapShare(f:IndexEntry,[w,s,e,n]:BBox){const [W,S,E,N]=f.bbox,ix=Math.max(0,Math.min(e,E)-Math.max(w,W)),iy=Math.max(0,Math.min(n,N)-Math.max(s,S));return ix*iy/Math.max((E-W)*(N-S),1e-12);}
export const byRelevance=(files:IndexEntry[],bbox:BBox)=>[...files].sort((a,b)=>overlapShare(b,bbox)-overlapShare(a,bbox));

// ---- footprint decoding: a port of compact_layer.decode ----
const metresPerDeg=(lat:number):[number,number]=>[111320*Math.cos(lat*(Math.PI/180)),110574];
// Vertex key of a ring position relative to its first vertex (exact while the offsets stay under 2^25, about 33 degrees).
const vkey=(x:number,y:number)=>(x+33554432)*67108864+(y+33554432);
// Splits a ring at repeated vertices into simple loops. A v2 keyhole ring (outer ring, then a zero-width slit to each
// hole and back) comes apart into the outer ring and its holes; the slits are loops of two vertices and are dropped.
function loops(c:number[]):number[][]{
 const n=c.length/2,stack:number[]=[],at=new Map<number,number>(),out:number[][]=[];
 for(let i=0;i<=n;i++){
  const x=c[2*(i%n)],y=c[2*(i%n)+1],key=vkey(x,y),j=at.get(key);
  if(j===undefined){at.set(key,stack.length/2);stack.push(x,y);continue;}
  const loop=stack.slice(2*j);for(let k=j+1;k<stack.length/2;k++)at.delete(vkey(stack[2*k],stack[2*k+1]));
  stack.length=2*(j+1);if(loop.length>=6)out.push(loop);
 }
 return out;
}
const area2=(r:number[])=>{let a=0;for(let i=0,n=r.length/2;i<n;i++){const j=(i+1)%n;a+=r[2*i]*r[2*j+1]-r[2*j]*r[2*i+1];}return a;};
function inside(x:number,y:number,r:number[]){let hit=false;for(let i=0,n=r.length/2,j=n-1;i<n;j=i++){const xi=r[2*i],yi=r[2*i+1],xj=r[2*j],yj=r[2*j+1];if((yi>y)!==(yj>y)&&x<(xj-xi)*(y-yi)/(yj-yi)+xi)hit=!hit;}return hit;}
// Relative integer ring -> lon/lat ring; the sum stays an exact integer before the division, as in Python.
const toRing=(r:number[],x0:number,y0:number):Ring=>{const out:Ring=[];for(let i=0;i<r.length;i+=2)out.push([(r[i]+x0)/1e6,(r[i+1]+y0)/1e6]);return out;};
export function decodeFootprint(lonE6:number,latE6:number,lengthDm:number,widthDm:number,angleCdeg:number,ring:Uint8Array|null):Footprint{
 if(!ring){
  const x0=lonE6/1e6,y0=latE6/1e6,[kx,ky]=metresPerDeg(y0),L=lengthDm/20,W=widthDm/20,a=angleCdeg/100*(Math.PI/180);
  const ux=Math.sin(a),uy=Math.cos(a),vx=-uy,vy=ux;               // long side direction (east, north) and its normal
  return{outer:([[-1,-1],[1,-1],[1,1],[-1,1]] as const).map(([sx,sy])=>[x0+(sx*L*ux+sy*W*vx)/kx,y0+(sx*L*uy+sy*W*vy)/ky] as [number,number]),holes:[]};
 }
 const view=new DataView(ring.buffer,ring.byteOffset,ring.byteLength),n=ring.byteLength>>2,c=new Array<number>(2*n+2);
 let x=0,y=0,repeated=false;c[0]=0;c[1]=0;const seen=new Set<number>([vkey(0,0)]);
 for(let i=0;i<n;i++){x+=view.getInt16(4*i,true);y+=view.getInt16(4*i+2,true);c[2*i+2]=x;c[2*i+3]=y;const key=vkey(x,y);if(seen.has(key))repeated=true;else seen.add(key);}
 if(!repeated)return{outer:toRing(c,lonE6,latE6),holes:[]};
 // Repeated vertices: rebuild the holes. Like make_valid plus "largest part" in Python, loops outside the largest one are dropped.
 const parts=loops(c);if(!parts.length)return{outer:toRing(c,lonE6,latE6),holes:[]};
 let best=0;parts.forEach((p,i)=>{if(Math.abs(area2(p))>Math.abs(area2(parts[best])))best=i;});
 const outer=parts[best],onOuter=new Set<number>();for(let i=0;i<outer.length;i+=2)onOuter.add(vkey(outer[i],outer[i+1]));
 const holes=parts.filter((p,i)=>{if(i===best)return false;for(let k=0;k<p.length;k+=2)if(!onOuter.has(vkey(p[k],p[k+1])))return inside(p[k],p[k+1],outer);return false;});
 return{outer:toRing(outer,lonE6,latE6),holes:holes.map(h=>toRing(h,lonE6,latE6))};
}

// ---- a decoded file: columns in typed arrays plus a grid over the buildings ----
export interface BuildingFile{
 name:string;n:number;id:Uint32Array;src:Uint8Array;srcNames:string[];lon:Int32Array;lat:Int32Array;length:Uint16Array;width:Uint16Array;angle:Uint16Array;
 ringStart:Uint32Array;ringBytes:Uint8Array;                     // ring i is ringBytes[ringStart[i]..ringStart[i+1]); empty = rectangle
 height:Uint16Array;heightSrc:Uint8Array;heightSrcNames:string[];floors:Uint8Array;  // 0 = unknown (src and height src: 255 = unknown)
 box:Int32Array;                                                  // per building west, south, east, north in 1e-6 degrees
 grid:{x0:number;y0:number;nx:number;ny:number;start:Uint32Array;order:Uint32Array;large:Uint32Array};
 memory:number;cover:number[];                                 // coverage cells (see coverCells) holding a building
}
export interface ColumnRows{[column:string]:ArrayLike<any>}
export function ringOf(f:BuildingFile,i:number){const a=f.ringStart[i],b=f.ringStart[i+1];return b>a?f.ringBytes.subarray(a,b):null;}
export const footprintOf=(f:BuildingFile,i:number)=>decodeFootprint(f.lon[i],f.lat[i],f.length[i],f.width[i],f.angle[i],ringOf(f,i));

function codes(values:ArrayLike<any>,n:number):[Uint8Array,string[]]{const names:string[]=[],idx=new Map<string,number>(),out=new Uint8Array(n).fill(255);for(let i=0;i<n;i++){const v=values[i];if(v==null)continue;const s=String(v);let k=idx.get(s);if(k===undefined){k=names.length;idx.set(s,k);names.push(s);}out[i]=k;}return[out,names];}
const typed=<T extends Uint16Array|Uint8Array|Int32Array|Uint32Array>(make:new(n:number)=>T,values:ArrayLike<any>,n:number)=>{const out=new make(n);for(let i=0;i<n;i++){const v=values[i];if(v!=null)out[i]=Number(v);}return out;};
// Columns as read from a published file -> BuildingFile with per-building boxes and a grid index.
export function buildFile(name:string,cols:ColumnRows):BuildingFile{
 const n=cols.id?.length??0;
 for(const c of ['src','lon_e6','lat_e6','length_dm','width_dm','angle_cdeg','ring','height_dm','height_src','floors'])if(!cols[c]||cols[c].length!==n)throw new Error(`missing column ${c}`);
 const ringStart=new Uint32Array(n+1);let total=0;for(let i=0;i<n;i++){const r=cols.ring[i] as Uint8Array|null;total+=r?r.byteLength:0;ringStart[i+1]=total;}
 const ringBytes=new Uint8Array(total);for(let i=0;i<n;i++){const r=cols.ring[i] as Uint8Array|null;if(r)ringBytes.set(r,ringStart[i]);}
 const [src,srcNames]=codes(cols.src,n),[heightSrc,heightSrcNames]=codes(cols.height_src,n);
 const f:BuildingFile={name,n,id:typed(Uint32Array,cols.id,n),src,srcNames,lon:typed(Int32Array,cols.lon_e6,n),lat:typed(Int32Array,cols.lat_e6,n),length:typed(Uint16Array,cols.length_dm,n),width:typed(Uint16Array,cols.width_dm,n),angle:typed(Uint16Array,cols.angle_cdeg,n),ringStart,ringBytes,height:typed(Uint16Array,cols.height_dm,n),heightSrc,heightSrcNames,floors:typed(Uint8Array,cols.floors,n),box:new Int32Array(4*n),grid:{x0:0,y0:0,nx:0,ny:0,start:new Uint32Array(1),order:new Uint32Array(0),large:new Uint32Array(0)},memory:0,cover:[]};
 let W=Infinity,S=Infinity,E=-Infinity,N=-Infinity;
 for(let i=0;i<n;i++){
  let w=Infinity,s=Infinity,e=-Infinity,no=-Infinity;
  for(const [x,y] of footprintOf(f,i).outer){const X=Math.round(x*1e6),Y=Math.round(y*1e6);if(X<w)w=X;if(X>e)e=X;if(Y<s)s=Y;if(Y>no)no=Y;}
  f.box.set([w,s,e,no],4*i);if(w<W)W=w;if(s<S)S=s;if(e>E)E=e;if(no>N)N=no;
 }
 // Grid by box centre; a building larger than one cell goes to `large` and is tested on every query.
 const cell=GRID_DEG*1e6,x0=n?W:0,y0=n?S:0,nx=n?Math.floor((E-W)/cell)+1:0,ny=n?Math.floor((N-S)/cell)+1:0;
 const cellOf=new Int32Array(n),count=new Uint32Array(nx*ny+1),large:number[]=[];
 for(let i=0;i<n;i++){const b=f.box.subarray(4*i,4*i+4);if(b[2]-b[0]>cell||b[3]-b[1]>cell){cellOf[i]=-1;large.push(i);continue;}const c=Math.floor(((b[1]+b[3])/2-y0)/cell)*nx+Math.floor(((b[0]+b[2])/2-x0)/cell);cellOf[i]=c;count[c+1]++;}
 for(let c=0;c<nx*ny;c++)count[c+1]+=count[c];
 const order=new Uint32Array(n-large.length),fill=count.slice(0,nx*ny);for(let i=0;i<n;i++)if(cellOf[i]>=0)order[fill[cellOf[i]]++]=i;
 f.grid={x0,y0,nx,ny,start:count,order,large:Uint32Array.from(large)};
 f.cover=coverCells(f);
 f.memory=[f.id,f.src,f.lon,f.lat,f.length,f.width,f.angle,f.ringStart,f.ringBytes,f.height,f.heightSrc,f.floors,f.box,count,order].reduce((v,a)=>v+a.byteLength,0);
 return f;
}
// Indices of the buildings whose box meets `bbox` (degrees).
export function buildingsIn(f:BuildingFile,bbox:BBox):number[]{
 const [w,s,e,n]=bbox.map(v=>v*1e6),{x0,y0,nx,ny,start,order,large}=f.grid,cell=GRID_DEG*1e6,out:number[]=[];
 const hit=(i:number)=>f.box[4*i]<=e&&f.box[4*i+2]>=w&&f.box[4*i+1]<=n&&f.box[4*i+3]>=s;
 // A centre lies at most half a cell outside its box, so one extra cell on each side is enough.
 const cx0=Math.max(0,Math.floor((w-x0)/cell)-1),cx1=Math.min(nx-1,Math.floor((e-x0)/cell)+1),cy0=Math.max(0,Math.floor((s-y0)/cell)-1),cy1=Math.min(ny-1,Math.floor((n-y0)/cell)+1);
 for(let cy=cy0;cy<=cy1;cy++)for(let cx=cx0;cx<=cx1;cx++){const c=cy*nx+cx;for(let k=start[c];k<start[c+1];k++)if(hit(order[k]))out.push(order[k]);}
 for(const i of large)if(hit(i))out.push(i);
 return out;
}
// Coverage: the 0.01-degree cells (about 1 km) a file has buildings in. Learnt when a file is first read and kept, so a
// file whose loose bounding box meets the view but whose buildings do not is not read again.
const COVER_DEG=.01,cellKey=(cx:number,cy:number)=>cx*100000+cy;
export function coverCells(f:Pick<BuildingFile,'n'|'box'>):number[]{const out=new Set<number>(),k=1e6*COVER_DEG;for(let i=0;i<f.n;i++){const b=f.box;for(let cx=Math.floor(b[4*i]/k);cx<=Math.floor(b[4*i+2]/k);cx++)for(let cy=Math.floor(b[4*i+1]/k);cy<=Math.floor(b[4*i+3]/k);cy++)out.add(cellKey(cx,cy));}return[...out].sort((a,b)=>a-b);}
export function coverMeets(cells:Set<number>,[w,s,e,n]:BBox){for(let cx=Math.floor(w/COVER_DEG);cx<=Math.floor(e/COVER_DEG);cx++)for(let cy=Math.floor(s/COVER_DEG);cy<=Math.floor(n/COVER_DEG);cy++)if(cells.has(cellKey(cx,cy)))return true;return false;}
export interface Selected{file:BuildingFile;i:number}
// Buildings of the loaded files that meet `bbox`, nearest the centre of `view` first when there are more than `cap`.
export function selectBuildings(files:BuildingFile[],bbox:BBox,view:BBox,cap=MAX_DRAWN_BUILDINGS):{items:Selected[];total:number}{
 const items:Selected[]=[];for(const file of files)for(const i of buildingsIn(file,bbox))items.push({file,i});
 const total=items.length;if(total<=cap)return{items,total};
 const cx=(view[0]+view[2])/2*1e6,cy=(view[1]+view[3])/2*1e6,k=Math.cos(cy/1e6*Math.PI/180);
 const d=({file,i}:Selected)=>{const b=file.box;const dx=((b[4*i]+b[4*i+2])/2-cx)*k,dy=(b[4*i+1]+b[4*i+3])/2-cy;return dx*dx+dy*dy;};
 return{items:items.map(s=>[d(s),s] as const).sort((a,b)=>a[0]-b[0]).slice(0,cap).map(x=>x[1]),total};
}

// ---- colours and popup text ----
export const UNKNOWN_BUILDING_COLOR='#9aa3a8';
export const BUILDING_BINS:{min:number;label:string;color:string}[]=[{min:1,label:'1–2',color:'#86b6ef'},{min:3,label:'3–4',color:'#5598e7'},{min:5,label:'5–7',color:'#2a78d6'},{min:8,label:'8–14',color:'#1c5cab'},{min:15,label:'≥ 15',color:'#0d366b'}];
// Floors, or the height at 3.2 m a floor when only the height is known; null when neither is.
export function floorsOf(f:BuildingFile,i:number){return f.floors[i]?f.floors[i]:f.height[i]?Math.max(1,Math.round(f.height[i]/32)):null;}
export function floorColor(floors:number|null){if(!floors)return UNKNOWN_BUILDING_COLOR;let c=BUILDING_BINS[0].color;for(const b of BUILDING_BINS)if(floors>=b.min)c=b.color;return c;}
const SRC_LABEL:Record<string,string>={osm:'OSM',google:'Google',microsoft:'Microsoft'},HSRC_LABEL:Record<string,string>={osm:'OSM',osm_part:'OSM (khối nhà)',g25:'Google 2.5D',g25cal:'Google, đã hiệu chỉnh',est:'ước tính từ nhà lân cận',wikidata:'Wikidata',sibling:'theo tòa cùng cụm',frontrow:'nhà mặt đường, tối thiểu 3 tầng'};
const vn=(v:number,d=0)=>v.toLocaleString('vi-VN',{minimumFractionDigits:d,maximumFractionDigits:d});
export const mb=(bytes:number)=>vn(bytes/1e6,1);
export function buildingLines(f:BuildingFile,i:number):string[]{
 const src=f.srcNames[f.src[i]],hs=f.heightSrcNames[f.heightSrc[i]];
 return[`Nhà #${f.id[i]}`,`Số tầng: ${f.floors[i]?f.floors[i]:'chưa rõ'}`,`Chiều cao: ${f.height[i]?`${vn(f.height[i]/10,1)} m${hs?` (${HSRC_LABEL[hs]||hs})`:''}`:'chưa rõ'}`,`Nguồn hình: ${src?SRC_LABEL[src]||src:'chưa rõ'}`];
}

// ---- reading and caching files ----
const zstd={ZSTD:(input:Uint8Array,length:number)=>decompressZstd(input,new Uint8Array(length))};
export async function readBuildingParquet(name:string,buf:ArrayBuffer):Promise<BuildingFile>{
 const parts:Record<string,{rowStart:number;data:ArrayLike<any>}[]>={};
 await parquetRead({file:buf,utf8:false,compressors:zstd,onChunk:({columnName,columnData,rowStart})=>{(parts[columnName]||=[]).push({rowStart,data:columnData});}});
 const cols:ColumnRows={};
 for(const [c,chunks] of Object.entries(parts)){if(chunks.length===1&&chunks[0].rowStart===0){cols[c]=chunks[0].data;continue;}const all:any[]=[];for(const {rowStart,data} of chunks)for(let k=0;k<data.length;k++)all[rowStart+k]=data[k];cols[c]=all;}
 return buildFile(name,cols);
}
export class BuildingError extends Error{}
export interface StoreDeps{fetch?:typeof fetch;caches?:CacheStorage|null;base?:string;memoryLimit?:number}
// Session store: the index (fetched once, revalidated by ETag, last copy kept for offline use), raw files kept in Cache
// Storage across reloads together with the index entry they were downloaded for, and decoded files in an LRU by memory.
// Cache Storage can be missing or refuse access (private mode, blocked storage); then files are simply downloaded.
export class BuildingStore{
 private memo:Promise<BuildingIndex>|null=null;private lru=new Map<string,BuildingFile>();private used=0;
 private cover=new Map<string,{bytes:number;buildings:number;cells:Set<number>}>();
 constructor(private deps:StoreDeps={}){}
 get base(){return this.deps.base??BUILDINGS_BASE;}
 private get fetch(){return this.deps.fetch??globalThis.fetch.bind(globalThis);}
 private async cache():Promise<Cache|null>{try{const c=this.deps.caches!==undefined?this.deps.caches:(globalThis as any).caches as CacheStorage|undefined;return c?await c.open(CACHE_NAME):null;}catch{return null;}}
 index():Promise<BuildingIndex>{if(!this.memo){this.memo=this.loadIndex();this.memo.catch(()=>{this.memo=null;});}return this.memo;}
 private async loadIndex():Promise<BuildingIndex>{
  const url=this.base+'index.json',cache=await this.cache();
  const parse=(text:string)=>{const v=JSON.parse(text);if(!v||!Array.isArray(v.files))throw new Error('bad index');return v as BuildingIndex;};
  try{
   const res=await this.fetch(url,{cache:'no-cache'});if(!res.ok)throw new Error(`HTTP ${res.status}`);
   const text=await res.text(),index=parse(text);
   if(cache)try{await cache.put(url,new Response(text,{headers:{'content-type':'application/json','x-size':String(text.length)}}));}catch{/* storage full or blocked */}
   await this.loadCover(cache);return index;
  }catch{
   try{const hit=await cache?.match(url);if(hit){const index={...parse(await hit.text()),offline:true};await this.loadCover(cache);return index;}}catch{/* storage unavailable */}
   throw new BuildingError('Không tải được danh sách nhà từ GitHub. Kiểm tra kết nối mạng.');
  }
 }
 cached(name:string){return this.lru.get(name);}
 private get coverUrl(){return this.base+'coverage.json';}
 private async loadCover(cache:Cache|null){try{const hit=await cache?.match(this.coverUrl);if(!hit)return;for(const [k,v] of Object.entries(await hit.json() as Record<string,any>))if(!this.cover.has(k)&&Array.isArray(v?.cells))this.cover.set(k,{bytes:v.bytes,buildings:v.buildings,cells:new Set(v.cells)});}catch{/* storage unavailable or bad entry */}}
 private async learnCover(entry:IndexEntry,f:BuildingFile,cache:Cache|null){
  this.cover.set(entry.file,{bytes:entry.bytes,buildings:entry.buildings,cells:new Set(f.cover)});if(!cache)return;
  const out:Record<string,unknown>={};for(const [k,v] of this.cover)out[k]={bytes:v.bytes,buildings:v.buildings,cells:[...v.cells]};
  try{const text=JSON.stringify(out);await cache.put(this.coverUrl,new Response(text,{headers:{'content-type':'application/json','x-size':String(text.length)}}));}catch{/* storage full or blocked */}
 }
 // Files the view needs, most relevant first: bounding box meets it, and the learnt coverage (when known for this index
 // entry) does too.
 needed(index:BuildingIndex,bbox:BBox){return byRelevance(filesForBBox(index.files,bbox),bbox).filter(e=>{const c=this.cover.get(e.file);return !c||c.bytes!==e.bytes||c.buildings!==e.buildings||coverMeets(c.cells,bbox);});}
 private remember(f:BuildingFile){this.lru.delete(f.name);this.lru.set(f.name,f);this.used=[...this.lru.values()].reduce((v,x)=>v+x.memory,0);const limit=this.deps.memoryLimit??MEMORY_LIMIT;for(const [k,v] of this.lru){if(this.used<=limit||this.lru.size<=1)break;this.lru.delete(k);this.used-=v.memory;}}
 // A decoded file: from memory, else from Cache Storage when stored for this same index entry, else downloaded.
 async file(entry:IndexEntry,signal?:AbortSignal,onDownload?:()=>void):Promise<BuildingFile>{
  const mem=this.lru.get(entry.file);if(mem){this.remember(mem);return mem;}
  const url=this.base+entry.file,cache=await this.cache();
  if(cache)try{
   const hit=await cache.match(url);
   if(hit&&hit.headers.get('x-bytes')===String(entry.bytes)&&hit.headers.get('x-buildings')===String(entry.buildings)){const f=await readBuildingParquet(entry.file,await hit.arrayBuffer());this.remember(f);const c=this.cover.get(entry.file);if(!c||c.bytes!==entry.bytes||c.buildings!==entry.buildings)await this.learnCover(entry,f,cache);return f;}
   if(hit)await cache.delete(url);
  }catch{try{await cache.delete(url);}catch{/* storage unavailable */}}
  if(signal?.aborted)throw new DOMException('aborted','AbortError');
  onDownload?.();
  let buf:ArrayBuffer;
  try{const res=await this.fetch(url,{signal});if(!res.ok)throw new BuildingError(`Không tải được nhà: ${entry.file} (lỗi HTTP ${res.status}).`);buf=await res.arrayBuffer();}
  catch(e){if(signal?.aborted||(e as Error)?.name==='AbortError')throw e;throw e instanceof BuildingError?e:new BuildingError(`Không tải được nhà: ${entry.file}. Kiểm tra kết nối mạng.`);}
  let f:BuildingFile;try{f=await readBuildingParquet(entry.file,buf);}catch{throw new BuildingError(`Không đọc được tệp nhà ${entry.file}.`);}
  if(cache)try{await cache.put(url,new Response(buf,{headers:{'content-type':'application/octet-stream','x-bytes':String(entry.bytes),'x-buildings':String(entry.buildings),'x-size':String(buf.byteLength)}}));}catch{/* storage full or blocked */}
  this.remember(f);await this.learnCover(entry,f,cache);return f;
 }
 // Bytes kept in Cache Storage, or null when it is unavailable.
 async storedBytes():Promise<number|null>{
  const cache=await this.cache();if(!cache)return null;
  try{let total=0;for(const req of await cache.keys()){const hit=await cache.match(req);total+=Number(hit?.headers.get('x-size'))||0;}return total;}catch{return null;}
 }
 // Drops the stored files; decoded files in memory stay until evicted.
 async clear(){try{const c=this.deps.caches!==undefined?this.deps.caches:(globalThis as any).caches as CacheStorage|undefined;await c?.delete(CACHE_NAME);}catch{/* storage unavailable */}}
}
export const buildingStore=new BuildingStore();
