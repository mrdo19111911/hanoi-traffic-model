import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {describe,it,expect,vi} from 'vitest';
import fixture from './test/buildings_fixture.json';
import {BuildingStore,byRelevance,BUILDINGS_BASE,MIN_BUILDING_ZOOM,buildFile,buildingLines,buildingsIn,buildingsShown,decodeFootprint,filesForBBox,floorColor,footprintOf,padBBox,readBuildingParquet,selectBuildings,BUILDING_BINS,UNKNOWN_BUILDING_COLOR,type BBox,type IndexEntry,type Ring} from './buildings';

// Published files committed in the repository (the same v1 files as on GitHub main).
const published=(name:string)=>{const b=readFileSync(resolve(process.cwd(),'../buildings/by_area',name));return new Uint8Array(b).buffer as ArrayBuffer;};
const hex=(s:string|null)=>s==null?null:Uint8Array.from(s.match(/../g)!.map(h=>parseInt(h,16)));
type Row=typeof fixture.rows[number];
function expectRing(got:Ring,want:number[][],ordered:boolean){
 expect(got).toHaveLength(want.length);
 const sort=(r:number[][])=>[...r].sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
 const [g,w]=ordered?[got,want]:[sort(got),sort(want)];
 g.forEach((p,k)=>{expect(Math.abs(p[0]-w[k][0])).toBeLessThan(1e-7);expect(Math.abs(p[1]-w[k][1])).toBeLessThan(1e-7);});
}
// Python's make_valid may start a rebuilt keyhole ring elsewhere, so rings with holes are compared as vertex sets.
function expectFootprint(fp:ReturnType<typeof decodeFootprint>,want:Row['expected']){
 const ordered=!want.holes.length;
 expectRing(fp.outer,want.outer,ordered);
 expect(fp.holes).toHaveLength(want.holes.length);
 const key=(r:number[][])=>Math.min(...r.map(p=>p[0]))*1e3+Math.min(...r.map(p=>p[1]));
 const holes=[...fp.holes].sort((a,b)=>key(a)-key(b)),wantHoles=[...want.holes].sort((a,b)=>key(a)-key(b));
 holes.forEach((h,k)=>expectRing(h,wantHoles[k],false));
}

describe('footprint decoding matches buildings/compact_layer.py',()=>{
 it('covers rectangles, rings, every source and v2 keyhole rings',()=>{
  const rows=fixture.rows;
  expect(rows.length).toBeGreaterThanOrEqual(50);
  expect(new Set(rows.map(r=>r.row.src))).toEqual(new Set(['osm','google','microsoft']));
  expect(rows.some(r=>r.row.ring==null)&&rows.some(r=>r.row.ring!=null)&&rows.some(r=>r.expected.holes.length===2)).toBe(true);
 });
 it('decodes every fixture row to the Python coordinates within 1e-7 degrees',()=>{
  for(const {row,expected} of fixture.rows)expectFootprint(decodeFootprint(row.lon_e6,row.lat_e6,row.length_dm,row.width_dm,row.angle_cdeg,hex(row.ring)),expected);
 });
 it('reads a published zstd parquet file and decodes the same rows',async()=>{
  const f=await readBuildingParquet(fixture.file,published(fixture.file));
  const at=new Map<number,number>();for(let i=0;i<f.n;i++)at.set(f.id[i],i);
  let checked=0;
  for(const {row,expected} of fixture.rows){
   const i=at.get(row.id);if(i===undefined)continue;   // the synthetic keyhole rows are not in the file
   expectFootprint(footprintOf(f,i),expected);
   expect(f.srcNames[f.src[i]]).toBe(row.src);
   expect(f.floors[i]).toBe(row.floors??0);expect(f.height[i]).toBe(row.height_dm??0);
   expect(f.heightSrcNames[f.heightSrc[i]]??null).toBe(row.height_src);
   checked++;
  }
  expect(checked).toBe(fixture.rows.length-2);
 });
});

const entry=(file:string,bbox:BBox,extra:Partial<IndexEntry>={}):IndexEntry=>({file,buildings:10,bytes:100,bbox,...extra});
describe('file selection and zoom gating',()=>{
 const files=[entry('a.parquet',[105.80,21.00,105.84,21.03]),entry('b.parquet',[105.84,21.00,105.90,21.05]),entry('far.parquet',[106.5,20.0,106.6,20.1]),{file:'broken.parquet',buildings:1,bytes:1} as unknown as IndexEntry];
 it('picks the files whose bounding box meets the view',()=>{
  expect(filesForBBox(files,[105.81,21.01,105.83,21.02]).map(f=>f.file)).toEqual(['a.parquet']);
  expect(filesForBBox(files,[105.83,21.01,105.85,21.02]).map(f=>f.file)).toEqual(['a.parquet','b.parquet']);
  expect(filesForBBox(files,[105.84,21.03,105.845,21.04]).map(f=>f.file)).toEqual(['a.parquet','b.parquet']);   // touching edges count
  expect(filesForBBox(files,[104,19,104.1,19.1])).toEqual([]);
 });
 it('loads the files that sit in the view before wide ones that only reach into it',()=>{
  const wide=entry('wide.parquet',[105.5,20.5,106.5,21.5]),tight=entry('tight.parquet',[105.81,21.01,105.83,21.02]);
  expect(byRelevance([wide,tight],[105.80,21.00,105.84,21.03]).map(f=>f.file)).toEqual(['tight.parquet','wide.parquet']);
 });
 it('pads the view by a share of its size on every side',()=>{
  const p=padBBox([0,0,10,4],.2);[-2,-.8,12,4.8].forEach((v,k)=>expect(p[k]).toBeCloseTo(v));
 });
 it(`draws buildings only from zoom ${MIN_BUILDING_ZOOM}`,()=>{
  expect(MIN_BUILDING_ZOOM).toBe(16);
  expect(buildingsShown(null)).toBe(false);expect(buildingsShown(15)).toBe(false);expect(buildingsShown(15.9)).toBe(false);
  expect(buildingsShown(16)).toBe(true);expect(buildingsShown(18)).toBe(true);
 });
});

// Square rectangles of 10 m on a line of longitudes, plus one large ring.
function synthetic(lons:number[],big=false){
 const n=lons.length+(big?1:0),ring:(Uint8Array|null)[]=lons.map(()=>null);
 if(big){const r=new Uint8Array(12),v=new DataView(r.buffer);v.setInt16(0,30000,true);v.setInt16(4,0,true);v.setInt16(6,30000,true);v.setInt16(8,-30000,true);ring.push(r);}
 const lon=[...lons.map(x=>Math.round(x*1e6)),105_800_000];
 return buildFile('t.parquet',{id:Array.from({length:n},(_,i)=>i+1),src:Array(n).fill('osm'),lon_e6:lon.slice(0,n),lat_e6:Array(n).fill(21_000_000),length_dm:Array(n).fill(100),width_dm:Array(n).fill(100),angle_cdeg:Array(n).fill(0),ring,height_dm:[...Array(n-1).fill(95),null],height_src:[...Array(n-1).fill('g25'),null],floors:[...Array(n-1).fill(3),null]});
}
describe('spatial index and draw cap',()=>{
 it('returns exactly the buildings whose box meets the query',()=>{
  const lons=Array.from({length:200},(_,i)=>105.80+i*.0005);
  const f=synthetic(lons,true);
  const got=buildingsIn(f,[105.8099,20.9999,105.8201,21.0001]).sort((a,b)=>a-b);
  const want=lons.map((x,i)=>[x,i] as const).filter(([x])=>x+.0000482>=105.8099&&x-.0000482<=105.8201).map(([,i])=>i);
  expect(got).toEqual([...want,200]);        // the large ring (index 200) is found through its own list
  expect(buildingsIn(f,[106,22,106.1,22.1])).toEqual([]);
 });
 it('keeps the buildings nearest the view centre when over the cap',()=>{
  const f=synthetic(Array.from({length:50},(_,i)=>105.80+i*.001));
  const view:BBox=[105.80,20.99,105.849,21.01];
  const {items,total}=selectBuildings([f],view,view,10);
  expect(total).toBe(50);expect(items).toHaveLength(10);
  expect(items.map(s=>s.i).sort((a,b)=>a-b)).toEqual([20,21,22,23,24,25,26,27,28,29]);
  expect(selectBuildings([f],view,view,100).items).toHaveLength(50);
 });
 it('colours by floors with grey for unknown, and builds the popup lines',()=>{
  expect(floorColor(null)).toBe(UNKNOWN_BUILDING_COLOR);expect(floorColor(1)).toBe(BUILDING_BINS[0].color);expect(floorColor(4)).toBe(BUILDING_BINS[1].color);expect(floorColor(30)).toBe(BUILDING_BINS[4].color);
  const f=synthetic([105.8],true);
  expect(buildingLines(f,0)).toEqual(['Nhà #1','Số tầng: 3','Chiều cao: 9,5 m (Google 2.5D)','Nguồn hình: OSM']);
  expect(buildingLines(f,1)).toEqual(['Nhà #2','Số tầng: chưa rõ','Chiều cao: chưa rõ','Nguồn hình: OSM']);
 });
});

// Cache Storage stand-in: one cache, Responses cloned on every read.
function fakeCaches(){
 const stores=new Map<string,Map<string,Response>>();
 const url=(k:any)=>typeof k==='string'?k:k.url;
 const open=async(name:string)=>{if(!stores.has(name))stores.set(name,new Map());const s=stores.get(name)!;return{match:async(k:any)=>s.get(url(k))?.clone(),put:async(k:any,r:Response)=>{s.set(url(k),r);},delete:async(k:any)=>s.delete(url(k)),keys:async()=>[...s.keys()].map(u=>({url:u}))};};
 return{stores,caches:{open,delete:async(name:string)=>stores.delete(name)} as unknown as CacheStorage};
}
const SMALL='khanh_hoa_13.parquet';
function fakeFetch(index:{files:IndexEntry[]}){
 return vi.fn(async(url:string,init?:RequestInit)=>{
  if(url.endsWith('index.json')){expect(init?.cache).toBe('no-cache');return new Response(JSON.stringify(index));}
  if(url===BUILDINGS_BASE+SMALL)return new Response(published(SMALL));
  return new Response('missing',{status:404});
 });
}
const smallEntry=(extra:Partial<IndexEntry>={})=>entry(SMALL,[109,12,109.1,12.1],{buildings:170,bytes:7531,...extra});
const fileCalls=(f:ReturnType<typeof vi.fn>)=>f.mock.calls.filter(c=>!String(c[0]).endsWith('index.json')).length;

describe('building store',()=>{
 it('keeps downloaded files across sessions and never fetches a stored file again',async()=>{
  const {caches}=fakeCaches(),index={files:[smallEntry()]};
  const fetch1=fakeFetch(index),s1=new BuildingStore({fetch:fetch1 as any,caches});
  const onDownload=vi.fn();
  const f=await s1.file((await s1.index()).files[0],undefined,onDownload);
  expect(f.n).toBe(170);expect(fileCalls(fetch1)).toBe(1);expect(onDownload).toHaveBeenCalledTimes(1);
  await s1.file(index.files[0]);expect(fileCalls(fetch1)).toBe(1);           // memory
  const fetch2=fakeFetch(index),s2=new BuildingStore({fetch:fetch2 as any,caches});   // a reload
  expect((await s2.file((await s2.index()).files[0])).n).toBe(170);
  expect(fileCalls(fetch2)).toBe(0);
  expect(await s2.storedBytes()).toBeGreaterThanOrEqual(7531);
 });
 it('downloads a stored file again when its index entry changes',async()=>{
  const {caches}=fakeCaches();
  const s1=new BuildingStore({fetch:fakeFetch({files:[smallEntry()]}) as any,caches});
  await s1.file(smallEntry());
  const fetch2=fakeFetch({files:[smallEntry({bytes:7600})]}),s2=new BuildingStore({fetch:fetch2 as any,caches});
  await s2.file((await s2.index()).files[0]);
  expect(fileCalls(fetch2)).toBe(1);
  const fetch3=fakeFetch({files:[smallEntry({bytes:7600})]}),s3=new BuildingStore({fetch:fetch3 as any,caches});
  await s3.file((await s3.index()).files[0]);
  expect(fileCalls(fetch3)).toBe(0);                                         // stored again with the new entry
 });
 it('falls back to the last stored index when offline, so stored areas still show',async()=>{
  const {caches}=fakeCaches();
  const s1=new BuildingStore({fetch:fakeFetch({files:[smallEntry()]}) as any,caches});
  await s1.file((await s1.index()).files[0]);
  const offline=vi.fn(async()=>{throw new TypeError('Failed to fetch');}),s2=new BuildingStore({fetch:offline as any,caches});
  const index=await s2.index();
  expect(index.offline).toBe(true);expect(index.files.map(f=>f.file)).toEqual([SMALL]);
  expect((await s2.file(index.files[0])).n).toBe(170);
  expect(offline).toHaveBeenCalledTimes(1);
  // Nothing stored and offline: a Vietnamese error, and a later call tries the network again.
  const s3=new BuildingStore({fetch:offline as any,caches:fakeCaches().caches});
  await expect(s3.index()).rejects.toThrow('Không tải được danh sách nhà');
  await expect(s3.index()).rejects.toThrow();expect(offline).toHaveBeenCalledTimes(3);
 });
 it('still downloads when storage is blocked, and reports HTTP errors in Vietnamese',async()=>{
  const blocked={open:async()=>{throw new DOMException('denied','SecurityError');},delete:async()=>{throw new Error('denied');}} as unknown as CacheStorage;
  const fetch=fakeFetch({files:[smallEntry()]}),s=new BuildingStore({fetch:fetch as any,caches:blocked});
  expect((await s.file(smallEntry())).n).toBe(170);
  expect(await s.storedBytes()).toBeNull();await s.clear();
  await expect(s.file(entry('nope.parquet',[0,0,1,1]))).rejects.toThrow('Không tải được nhà: nope.parquet (lỗi HTTP 404).');
 });
 it('clears the stored files, and evicts decoded files beyond the memory limit',async()=>{
  const {caches,stores}=fakeCaches();
  const s=new BuildingStore({fetch:fakeFetch({files:[smallEntry()]}) as any,caches,memoryLimit:1});
  await s.file(smallEntry());await s.file(smallEntry({file:'copy.parquet'})).catch(()=>null);
  expect(await s.storedBytes()).toBeGreaterThan(0);
  await s.clear();expect(stores.size).toBe(0);
  const t=new BuildingStore({fetch:vi.fn(async(url:string)=>new Response(published(SMALL))) as any,caches:null,memoryLimit:1});
  await t.file(smallEntry());await t.file(smallEntry({file:'other.parquet'}));
  expect(t.cached(SMALL)).toBeUndefined();expect(t.cached('other.parquet')).toBeDefined();   // the newest file always stays
 });
 it('learns where a file has buildings, so a loose bounding box does not make it load again',async()=>{
  const {caches}=fakeCaches(),loose=smallEntry({bbox:[100,5,115,25]}),index={files:[loose]};
  const s1=new BuildingStore({fetch:fakeFetch(index) as any,caches});
  const idx=await s1.index(),hanoi:BBox=[105.80,21.00,105.81,21.01];
  expect(s1.needed(idx,hanoi)).toHaveLength(1);                              // coverage not known yet
  const f=await s1.file(idx.files[0]);
  expect(s1.needed(idx,hanoi)).toEqual([]);
  const b=f.box;expect(s1.needed(idx,[b[0]/1e6,b[1]/1e6,b[2]/1e6,b[3]/1e6])).toHaveLength(1);
  const s2=new BuildingStore({fetch:fakeFetch(index) as any,caches});      // a reload reads the stored coverage
  expect(s2.needed(await s2.index(),hanoi)).toEqual([]);
  const s3=new BuildingStore({fetch:fakeFetch({files:[{...loose,bytes:8000}]}) as any,caches});
  expect(s3.needed(await s3.index(),hanoi)).toHaveLength(1);                 // a changed entry forgets it
 });
 it('stops a download that is no longer needed',async()=>{
  const ctrl=new AbortController();
  const fetch=vi.fn((_url:string,init?:RequestInit)=>new Promise<Response>((_,reject)=>init!.signal!.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')))));
  const s=new BuildingStore({fetch:fetch as any,caches:null});
  const p=s.file(smallEntry(),ctrl.signal);ctrl.abort();
  await expect(p).rejects.toMatchObject({name:'AbortError'});
 });
});
