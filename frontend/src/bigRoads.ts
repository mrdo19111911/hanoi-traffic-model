// Pure helpers for the big-road (network A) review map. No DOM, no Leaflet.
import type {Feature} from 'geojson';
import type {BigRoadMeta,BigRoadClusters,ClusterPartition,ClusterStats,ReviewEntry,ReviewStatus} from './api';

export const GROUPS=['caotoc','quoclo','tinhlo'] as const;
export type Group=typeof GROUPS[number];
export const groupLabels:Record<Group,string>={caotoc:'Cao tốc',quoclo:'Quốc lộ',tinhlo:'Tỉnh lộ'};
export const deadEndReasons:Record<string,string>={construction:'Đường đang xây',continues:'Đường chạy tiếp nhưng OSRM không nối được trong 2 km',end:'Hết đường trong OSM'};
export const reviewStatusLabels:Record<ReviewStatus,string>={unseen:'chưa xem',seen:'đã xem',skipped:'bỏ qua'};
export const reviewKindLabels:Record<ReviewKind,string>={deadend:'Điểm cụt',ferry:'Phà chưa xác nhận',osrm:'Lỗi OSRM'};
export const bridgeKindLabels:Record<string,string>={road:'Nối bù qua đường lớn thiếu số hiệu',small:'Nối bù qua đường nhỏ (theo OSRM)',ferry:'Phà'};
export const speedSourceLabels:Record<string,string>={osm_maxspeed:'maxspeed của OSM × 0,8',profile_default:'mặc định theo profile OSRM',ferry:'chiều dài / thời gian phà',unknown:'không rõ'};
const ferrySource:Record<string,string>={osm:'theo OSM',osrm_5kmh:'OSRM ước tính 5 km/h'};
export const ferryTipLines=(p:any):string[]=>[p.ferry_name||'Phà',p.ferry_min!=null?`khoảng ${p.ferry_min} phút`:'',ferrySource[p.ferry_min_source]||'',p.car_confirmed?'':'OSM chưa ghi chở ô tô'].filter(Boolean);
const props=(f:Feature)=>(f.properties||{}) as Record<string,any>;

// Refs are compared without dots and spaces, case-insensitively, with DT and TL read as ĐT.
export function normalizeRef(ref:string):string{
 const compact=ref.replace(/[.\s]/g,'').toUpperCase();
 return compact.replace(/^(DT|TL)(?=\d|$)/,'ĐT');
}
export const refParts=(ref:unknown):string[]=>typeof ref==='string'?ref.split(';').map(s=>s.trim()).filter(Boolean):[];
export const refMatches=(ref:unknown,query:string)=>{const q=normalizeRef(query);return !!q&&refParts(ref).some(part=>normalizeRef(part)===q);};
export function foldText(text:string):string{return text.normalize('NFD').replace(/[̀-ͯ]/g,'').replace(/[đĐ]/g,'d').toLowerCase().trim();}

export interface RouteHit{id:string;kind:'ref'|'name';key:string;label:string;km:number}
export function routeMatches(feature:Feature,hit:Pick<RouteHit,'kind'|'key'>):boolean{
 const p=props(feature);
 return hit.kind==='ref'?refParts(p.ref).some(part=>normalizeRef(part)===hit.key):!refParts(p.ref).length&&foldText(String(p.name||''))===hit.key;
}
// A route is every road sharing one ref; roads without a ref are grouped by their name.
export function searchRoutes(features:Feature[],query:string,limit=10):RouteHit[]{
 const q=query.trim();if(!q)return [];
 const qRef=normalizeRef(q),qName=foldText(q);
 const entries=new Map<string,RouteHit&{names:Set<string>}>();
 for(const f of features){
  const p=props(f),name=String(p.name||''),km=Number(p.km)||0,refs=refParts(p.ref);
  const keys=refs.length?refs.map(r=>({kind:'ref' as const,key:normalizeRef(r),label:r})):name?[{kind:'name' as const,key:foldText(name),label:name}]:[];
  for(const k of keys){
   const id=`${k.kind}:${k.key}`;let e=entries.get(id);
   if(!e){e={id,kind:k.kind,key:k.key,label:k.label,km:0,names:new Set()};entries.set(id,e);}
   e.km+=km;if(name)e.names.add(foldText(name));
  }
 }
 const byRef:RouteHit[]=[],byName:RouteHit[]=[];
 for(const e of entries.values()){
  const hit={id:e.id,kind:e.kind,key:e.key,label:e.label,km:Math.round(e.km*1000)/1000};
  if(e.kind==='ref'&&e.key===qRef)byRef.push(hit);
  else if(qName&&[...e.names].some(n=>n.includes(qName)))byName.push(hit);
 }
 byName.sort((a,b)=>b.km-a.km);
 return [...byRef,...byName].slice(0,limit);
}

const vertexKey=([lon,lat]:number[])=>`${lon.toFixed(6)},${lat.toFixed(6)}`;
const lineCoords=(f:Feature):number[][]=>f.geometry?.type==='LineString'?f.geometry.coordinates:[];
export interface RouteStats{features:Feature[];km:number;pieces:number;deadEnds:number;bbox:[number,number,number,number]|null}
// Pieces are groups of route roads that share at least one vertex.
export function routeStats(roads:Feature[],junctions:Feature[],hit:Pick<RouteHit,'kind'|'key'>):RouteStats{
 const features=roads.filter(f=>routeMatches(f,hit));
 const parent=features.map((_,i)=>i);const find=(i:number):number=>parent[i]===i?i:(parent[i]=find(parent[i]));
 const owner=new Map<string,number>();let west=180,south=90,east=-180,north=-90,km=0;
 features.forEach((f,i)=>{km+=Number(props(f).km)||0;for(const c of lineCoords(f)){west=Math.min(west,c[0]);east=Math.max(east,c[0]);south=Math.min(south,c[1]);north=Math.max(north,c[1]);const k=vertexKey(c),o=owner.get(k);if(o==null)owner.set(k,i);else parent[find(i)]=find(o);}});
 const pieces=new Set(features.map((_,i)=>find(i))).size;
 const deadEnds=junctions.filter(j=>props(j).type==='deadend'&&j.geometry?.type==='Point'&&owner.has(vertexKey(j.geometry.coordinates))).length;
 return {features,km:Math.round(km*1000)/1000,pieces,deadEnds,bbox:features.length&&west<=east?[west,south,east,north]:null};
}

export function lineKm(coords:number[][]):number{
 const R=6371.0088,rad=Math.PI/180;let total=0;
 for(let i=1;i<coords.length;i++){const [lon1,lat1]=coords[i-1],[lon2,lat2]=coords[i];const dLat=(lat2-lat1)*rad,dLon=(lon2-lon1)*rad;const a=Math.sin(dLat/2)**2+Math.cos(lat1*rad)*Math.cos(lat2*rad)*Math.sin(dLon/2)**2;total+=2*R*Math.asin(Math.min(1,Math.sqrt(a)));}
 return total;
}

export const UNKNOWN_COLOR='#9aa5ab';
export const SPEED_BANDS=[{below:30,color:'#c0392b',label:'dưới 30'},{below:50,color:'#e67e22',label:'30–50'},{below:70,color:'#f1c40f',label:'50–70'},{below:90,color:'#7fb800',label:'70–90'},{below:Infinity,color:'#1e8449',label:'từ 90'}];
export function speedColor(kmh:number|null|undefined):string{
 if(kmh==null||!Number.isFinite(kmh))return UNKNOWN_COLOR;
 return SPEED_BANDS.find(b=>kmh<b.below)!.color;
}
export const CLUSTER_COLORS=['#1f77b4','#ff7f0e','#2ca02c','#d62728','#9467bd','#8c564b','#e377c2','#17becf','#bcbd22','#393b79','#637939','#843c39'];
export function clusterColor(cluster:number|null|undefined):string{
 if(cluster==null||!Number.isFinite(cluster))return UNKNOWN_COLOR;
 const n=CLUSTER_COLORS.length;return CLUSTER_COLORS[((Math.trunc(cluster)%n)+n)%n];
}

// Bridges and ferries carry the group of the road they join, so the chips hide them with it.
export function visibleGroups(features:Feature[],groups:Record<Group,boolean>,ferries:boolean):Feature[]{
 return features.filter(f=>{const p=props(f);const g=(p.group in groups?p.group:'quoclo') as Group;return groups[g]&&(ferries||p.bridge_kind!=='ferry');});
}

export type ReviewKind='deadend'|'ferry'|'osrm';
export interface ReviewItem{id:string;kind:ReviewKind;key:string;route:string;reason:string;status:ReviewStatus;note:string;at:[number,number];mainline:boolean;osmType:'node'|'way';osmId:number|string}
export function reviewItems(data:{junctions:Feature[];roads:Feature[];meta:BigRoadMeta},review:Record<string,ReviewEntry>):ReviewItem[]{
 const state=(key:string)=>({status:(review[key]?.status||'unseen') as ReviewStatus,note:review[key]?.note||''});
 const items:ReviewItem[]=[];
 for(const j of data.junctions){
  const p=props(j);if(p.type!=='deadend'||p.node_id==null||j.geometry?.type!=='Point')continue;
  const key=String(p.node_id),[lon,lat]=j.geometry.coordinates;
  items.push({id:`deadend:${key}`,kind:'deadend',key,route:String(p.big_road||''),reason:String(p.reason||''),...state(key),at:[lon,lat],mainline:!!p.mainline,osmType:'node',osmId:p.node_id});
 }
 const ferries=new Set<string>();
 for(const r of data.roads){
  const p=props(r);if(p.bridge_kind!=='ferry'||p.car_confirmed||p.way_id==null)continue;
  const key=`ferry:${p.way_id}`;if(ferries.has(key))continue;ferries.add(key);
  const coords=lineCoords(r);const mid=coords[Math.floor(coords.length/2)]||[0,0];
  items.push({id:key,kind:'ferry',key,route:String(p.ferry_name||'Phà'),reason:'unconfirmed',...state(key),at:[mid[0],mid[1]],mainline:true,osmType:'way',osmId:p.way_id});
 }
 const samples=Array.isArray(data.meta.osrm_error_samples)?data.meta.osrm_error_samples:[];
 for(const s of samples){
  if(s?.node_id==null||!Array.isArray(s.at))continue;
  const key=String(s.node_id);
  items.push({id:`osrm:${key}`,kind:'osrm',key,route:'',reason:String(s.error||''),...state(key),at:[Number(s.at[0]),Number(s.at[1])],mainline:true,osmType:'node',osmId:s.node_id});
 }
 return items;
}
export interface ReviewFilter{all:boolean;reason:string;status:string}
export function filterReview(items:ReviewItem[],filter:ReviewFilter):ReviewItem[]{
 return items.filter(i=>(filter.all||i.kind!=='deadend'||i.mainline)&&(!filter.reason||i.kind==='deadend'&&i.reason===filter.reason)&&(!filter.status||i.status===filter.status));
}
export const reviewCounts=(items:ReviewItem[])=>({left:items.filter(i=>i.status==='unseen').length,total:items.length});
export function reviewReasonText(item:ReviewItem):string{
 if(item.kind==='ferry')return 'OSM chưa ghi chở ô tô';
 if(item.kind==='osrm')return item.reason?`OSRM báo lỗi: ${item.reason}`:'OSRM báo lỗi';
 return deadEndReasons[item.reason]||'';
}
const OSM='https://www.openstreetmap.org';
export const osmViewUrl=(item:Pick<ReviewItem,'osmType'|'osmId'>)=>`${OSM}/${item.osmType}/${encodeURIComponent(String(item.osmId))}`;
export const osmEditUrl=(item:Pick<ReviewItem,'osmType'|'osmId'>)=>`${OSM}/edit?${item.osmType}=${encodeURIComponent(String(item.osmId))}`;
const csvField=(value:unknown)=>{const s=String(value??'');return /[",\r\n]/.test(s)?`"${s.replace(/"/g,'""')}"`:s;};
// Excel needs the BOM to read Vietnamese text in UTF-8.
export function reviewCsv(items:ReviewItem[]):string{
 const header=['loại','khóa','tuyến','lý do','trạng thái','ghi chú','kinh độ','vĩ độ'];
 const rows=items.map(i=>[reviewKindLabels[i.kind],i.key,i.route,i.kind==='osrm'?i.reason:reviewReasonText(i),reviewStatusLabels[i.status],i.note,i.at[0],i.at[1]]);
 return '﻿'+[header,...rows].map(r=>r.map(csvField).join(',')).join('\r\n');
}

// Spec 18.14: spectral partitions of network A, one per k.
const fmt=(n:number,digits:number)=>n.toLocaleString('vi-VN',{maximumFractionDigits:digits});
export const DEFAULT_K=16;
// Spec 19.4: a partition without `method` is spectral; the province partition is listed separately.
export const isSpectral=(p:ClusterPartition)=>(p.method??'spectral')==='spectral';
const spectralParts=(clusters:BigRoadClusters|null|undefined)=>(clusters?.partitions||[]).filter(isSpectral);
export const availableKs=(clusters:BigRoadClusters|null|undefined)=>[...new Set(spectralParts(clusters).map(p=>p.k))].sort((a,b)=>a-b);
export const provincePartition=(clusters:BigRoadClusters|null|undefined)=>(clusters?.partitions||[]).find(p=>p.method==='province')||null;
// Round 4: the partition along main rivers ("natural"), also listed separately.
export const naturalPartition=(clusters:BigRoadClusters|null|undefined)=>(clusters?.partitions||[]).find(p=>p.method==='natural')||null;
// Step 3: main-river regions split by spectral clustering ("natural_spectral").
export const twoLevelPartition=(clusters:BigRoadClusters|null|undefined)=>(clusters?.partitions||[]).find(p=>p.method==='natural_spectral')||null;
export type PartitionChoice=number|'province'|'natural'|'natural_spectral'|null;
export function pickPartition(clusters:BigRoadClusters|null|undefined,choice:PartitionChoice):ClusterPartition|null{
 if(choice==='province'){const province=provincePartition(clusters);if(province)return province;}
 if(choice==='natural'){const natural=naturalPartition(clusters);if(natural)return natural;}
 if(choice==='natural_spectral'){const two=twoLevelPartition(clusters);if(two)return two;}
 const parts=spectralParts(clusters);
 if(!parts.length)return provincePartition(clusters);
 return parts.find(p=>p.k===choice)||parts.find(p=>p.k===DEFAULT_K)||[...parts].sort((a,b)=>a.k-b.k)[0];
}
type Adjacency=Record<number,number[]>;
// Clusters of the ways meeting at each vertex, keyed like routeStats.
function vertexClusters(roads:Feature[],ways:Record<string,number>){
 const at=new Map<string,Map<number,number>>();
 for(const f of roads){const c=ways[String(props(f).way_id)];if(c==null)continue;const seen=new Set<string>();for(const v of lineCoords(f)){const key=vertexKey(v);if(seen.has(key))continue;seen.add(key);let m=at.get(key);if(!m){m=new Map();at.set(key,m);}m.set(c,(m.get(c)||0)+1);}}
 return at;
}
function addEdge(adj:Map<number,Set<number>>,a:number,b:number){if(a===b)return;adj.get(a)!.add(b);adj.get(b)!.add(a);}
function toAdjacency(adj:Map<number,Set<number>>):Adjacency{const out:Adjacency={};for(const c of [...adj.keys()].sort((a,b)=>a-b))out[c]=[...adj.get(c)!].sort((a,b)=>a-b);return out;}
const emptyAdjacency=(ways:Record<string,number>)=>new Map([...new Set(Object.values(ways))].map(c=>[c,new Set<number>()]));
// Two clusters are adjacent when ways of both meet at a vertex.
export function clusterAdjacency(roads:Feature[],ways:Record<string,number>):Adjacency{
 const adj=emptyAdjacency(ways);
 for(const m of vertexClusters(roads,ways).values()){const cs=[...m.keys()];for(let i=0;i<cs.length;i++)for(let j=i+1;j<cs.length;j++)addEdge(adj,cs[i],cs[j]);}
 return toAdjacency(adj);
}
// Most common cluster among the ways at a vertex, ties to the smallest id, optionally skipping one cluster.
function majority(m:Map<number,number>|undefined,skip?:number):number|null{let best:number|null=null,count=0;for(const [c,n] of m||[]){if(c===skip)continue;if(n>count||n===count&&best!=null&&c<best){best=c;count=n;}}return best;}
export interface CutInfo{coords:number[][];a:number|null;b:number|null;km:number;aName?:string;bName?:string}
export function cutInfo(partition:ClusterPartition,roads:Feature[]):CutInfo[]{
 const at=vertexClusters(roads,partition.ways);
 return (partition.cuts||[]).filter(c=>c.length>1).map(coords=>{
  const start=at.get(vertexKey(coords[0])),end=at.get(vertexKey(coords[coords.length-1]));
  // One side from each end; if the far end has no other cluster, the other cluster met at the start.
  let a=majority(start),b=majority(end,a??undefined);
  if(b==null)b=majority(start,a??undefined);
  if(a==null&&b!=null)a=majority(end,b);
  if(b==null)b=majority(end);
  const pair=a!=null&&b!=null&&a>b?[b,a]:[a,b];
  const name=(c:number|null)=>c==null?undefined:partition.names?.[String(c)];
  return {coords,a:pair[0],b:pair[1],km:lineKm(coords),aName:name(pair[0]),bName:name(pair[1])};
 });
}
const side=(name:string|undefined,id:number|null)=>name||`cụm ${id??'?'}`;
export const cutTipText=(cut:CutInfo)=>`Cắt giữa ${side(cut.aName,cut.a)} và ${side(cut.bName,cut.b)}, ${fmt(cut.km,1)} km`;
export const partitionLabel=(p:ClusterPartition)=>p.method==='province'?'Phân cụm · theo tỉnh':p.method==='natural'?'Phân cụm · theo sông':p.method==='natural_spectral'?'Phân cụm · sông + spectral':`Phân cụm · k = ${p.k}`;
// Adjacency from shared vertices plus the two sides of every cut.
export function partitionAdjacency(partition:ClusterPartition,roads:Feature[]):Adjacency{
 const adj=emptyAdjacency(partition.ways);
 for(const [c,ns] of Object.entries(clusterAdjacency(roads,partition.ways)))for(const n of ns)addEdge(adj,Number(c),n);
 for(const cut of cutInfo(partition,roads))if(cut.a!=null&&cut.b!=null&&adj.has(cut.a)&&adj.has(cut.b))addEdge(adj,cut.a,cut.b);
 return toAdjacency(adj);
}
// Greedy colouring, most-connected clusters first (ties by id), so adjacent clusters never share a colour
// while the 12-colour palette lasts; beyond that the palette cycles.
export function colorClusters(adjacency:Adjacency):Record<number,string>{
 const ids=Object.keys(adjacency).map(Number).sort((a,b)=>(adjacency[b].length-adjacency[a].length)||a-b);
 const slot:Record<number,number>={};
 for(const c of ids){const used=new Set(adjacency[c].map(n=>slot[n]).filter(s=>s!=null));let s=0;while(used.has(s)&&s<CLUSTER_COLORS.length)s++;slot[c]=s<CLUSTER_COLORS.length?s:c%CLUSTER_COLORS.length;}
 return Object.fromEntries(ids.map(c=>[c,CLUSTER_COLORS[slot[c]]]));
}
export function clusterStatRows(stats:Partial<ClusterStats>,k:number):[string,string][]{
 const n=(v:unknown,d=0)=>typeof v==='number'?fmt(v,d):'—';
 const contiguous=typeof stats.contiguous==='number'?`${fmt(stats.contiguous,0)} / ${fmt(k,0)}`:typeof stats.contiguous==='boolean'?(stats.contiguous?'có':'không'):String(stats.contiguous??'—');
 return [['Số cạnh bị cắt',n(stats.cut_edges)],['Km bị cắt',`${n(stats.cut_km)} km`],['Tỉ lệ km bị cắt',`${n(stats.cut_pct,1)} %`],['Số nút biên',n(stats.boundary_nodes)],['Số cụm liền mạch',contiguous],['Conductance trung bình',n(stats.conductance_mean,4)],['Km mỗi cụm (nhỏ nhất / trung vị / lớn nhất)',`${n(stats.km_min)} / ${n(stats.km_median)} / ${n(stats.km_max)}`],...(stats.unassigned_nodes!=null?[['Số nút chưa gán',n(stats.unassigned_nodes)] as [string,string]]:[])];
}
export const clustersOutdated=(clusters:BigRoadClusters|null|undefined,meta:BigRoadMeta)=>!!clusters?.pbf_sha256&&typeof meta.pbf_sha256==='string'&&clusters.pbf_sha256!==meta.pbf_sha256;

// Spec 19.6: spectral k = 32 against the province partition; lower is better except the contiguous share.
export interface CompareRow{label:string;spectral:string;province:string;better:'spectral'|'province'|null}
export function compareRows(spectral:ClusterPartition,province:ClusterPartition):CompareRow[]{
 const a=spectral.stats||{} as ClusterStats,b=province.stats||{} as ClusterStats;
 const n=(v:unknown,d=0)=>typeof v==='number'?fmt(v,d):'—';
 const pick=(x:unknown,y:unknown,higher=false):CompareRow['better']=>typeof x!=='number'||typeof y!=='number'||x===y?null:(x<y)!==higher?'spectral':'province';
 const share=(s:ClusterStats,k:number)=>typeof s.contiguous==='number'&&k?s.contiguous/k:undefined;
 const contiguous=(s:ClusterStats,k:number)=>typeof s.contiguous==='number'?`${fmt(s.contiguous,0)} / ${fmt(k,0)}`:'—';
 const km=(s:ClusterStats)=>`${n(s.cut_km)} km (${n(s.cut_pct,1)} %)`;
 const spread=(s:ClusterStats)=>`${n(s.km_min)} / ${n(s.km_median)} / ${n(s.km_max)}`;
 return [
  {label:'Số cạnh bị cắt',spectral:n(a.cut_edges),province:n(b.cut_edges),better:pick(a.cut_edges,b.cut_edges)},
  {label:'Km bị cắt (tỉ lệ)',spectral:km(a),province:km(b),better:pick(a.cut_km,b.cut_km)},
  {label:'Số nút biên',spectral:n(a.boundary_nodes),province:n(b.boundary_nodes),better:pick(a.boundary_nodes,b.boundary_nodes)},
  {label:'Số cụm liền mạch',spectral:contiguous(a,spectral.k),province:contiguous(b,province.k),better:pick(share(a,spectral.k),share(b,province.k),true)},
  {label:'Conductance trung bình',spectral:n(a.conductance_mean,4),province:n(b.conductance_mean,4),better:pick(a.conductance_mean,b.conductance_mean)},
  {label:'Km mỗi cụm (nhỏ nhất / trung vị / lớn nhất)',spectral:spread(a),province:spread(b),better:null},
  {label:'Số nút chưa gán',spectral:'—',province:n(b.unassigned_nodes),better:null},
 ];
}
