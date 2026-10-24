import {useEffect,useMemo,useRef,useState} from 'react';
import L from 'leaflet';
import type {Feature,Geometry,Polygon} from 'geojson';
import {CircleMarker,GeoJSON,MapContainer,TileLayer,Marker,Polyline,Tooltip,ZoomControl,useMap,useMapEvents} from 'react-leaflet';
import type {Point,Route,RoadClass,BigRoadView,ColorBy,MapFocus,TrialRoute,TrialPoints} from './api';
import {UNKNOWN_COLOR,cutTipText,ferryTipLines,speedColor,type CutInfo} from './bigRoads';
import {BuildingError,VIEW_MARGIN,buildingLines,buildingStore,buildingsShown,floorColor,floorsOf,footprintOf,padBBox,selectBuildings,type BBox,type BuildingFile,type IndexEntry} from './buildings';
export {ferryTipLines};

export type LatLon=[number,number];
interface Props{
 origin:Point;destination:Point;route:Route|null;classes:RoadClass[];picking:'origin'|'destination'|null;onPick:(point:Point)=>void;
 bigRoads?:BigRoadView|null;basemap?:boolean;onZoomChange?:(zoom:number)=>void;
 // Big-road mode only: a road or junction was clicked, or an empty part of the map.
 onRoadClick?:(feature:Feature,at:LatLon)=>void;onJunctionClick?:(feature:Feature,at:LatLon)=>void;onMapClick?:(at:LatLon)=>void;
 // Spec 20: changes whenever the drawer beside the map opens or closes, so the map refreshes its size.
 layoutKey?:string;
 // Building layer: drawn from zoom 16 with files read straight from GitHub; its state goes to onBuildingStatus.
 buildings?:boolean;onBuildingStatus?:(status:BuildingStatus)=>void;
}
export type BuildingStatus={kind:'zoom'}|{kind:'loading';file:string;bytes:number;network:boolean;shown:number}|{kind:'ready';shown:number;total:number;offline?:boolean}|{kind:'error';message:string;shown:number};
const endpointIcon=(letter:string,color:string)=>L.divIcon({className:'endpoint-icon',html:`<span style="background:${color}">${letter}</span>`,iconSize:[34,42],iconAnchor:[17,39]});
const aIcon=endpointIcon('A','#123b4a'),bIcon=endpointIcon('B','#e16d40');
const pulseIcon=L.divIcon({className:'pulse-point',iconSize:[26,26]});
// OSM names are external data: build tooltips from text nodes, never HTML.
function tip(lines:string[]){const box=document.createElement('div');lines.filter(Boolean).forEach((line,index)=>{if(index)box.appendChild(document.createElement('br'));box.appendChild(document.createTextNode(line));});return box;}

const groupColor:Record<string,string>={caotoc:'#dc3e48',quoclo:'#e89b36',tinhlo:'#d4b12f'},groupWeight:Record<string,number>={caotoc:3,quoclo:2.5,tinhlo:1.8};
export const FERRY_COLOR='#2f6fb5';
// Almost every provincial road is kept only by its ref, so the ref-only dash is for expressways and national roads.
// Ferries found through OSRM are drawn as their own dashed blue line, fainter when OSM does not say they carry cars.
export const roadStyle=(feature?:Feature<Geometry,any>)=>{
 const p=feature?.properties||{};
 if(p.isolated)return{color:'#8a949a',weight:2,opacity:.9};
 if(p.bridge_kind==='ferry')return{color:FERRY_COLOR,weight:4,opacity:p.car_confirmed?.9:.55,dashArray:'6 4'};
 const group=p.group in groupColor?p.group:'quoclo';
 return{color:groupColor[group],weight:groupWeight[group],opacity:.9,dashArray:p.bridge?'2 3':p.ref_only&&group!=='tinhlo'?'6 4':undefined};
};
export interface RoadStyleOptions{colorBy?:ColorBy;clusters?:Record<string,number>|null;clusterColors?:Record<number,string>|null;highlight?:Set<Feature>|null}
// Speed and cluster colouring replace only the colour; dashes and widths stay those of roadStyle.
export function makeRoadStyle({colorBy='group',clusters,clusterColors,highlight}:RoadStyleOptions){
 return (feature?:Feature<Geometry,any>)=>{
  const p=feature?.properties||{},base=roadStyle(feature);
  const color=colorBy==='speed'?speedColor(p.speed_kmh):colorBy==='cluster'?clusterColors?.[clusters?.[String(p.way_id)] as number]??UNKNOWN_COLOR:base.color;
  const style={...base,color};
  if(!highlight?.size)return style;
  return feature&&highlight.has(feature)?{...style,weight:base.weight*2}:{...style,opacity:.25};
 };
}
// Spec 18.14: cuts are black over a white casing so they stand out on every cluster colour.
export const cutStyle={color:'#000000',weight:4,opacity:1};
export const cutCasingStyle={color:'#ffffff',weight:8,opacity:1};
export const boundaryNodeStyle={radius:3,color:'#000000',weight:1,fillColor:'#000000',fillOpacity:1};
// Spec 19.6: province outlines, drawn under the roads; names are OSM text, shown as text nodes.
export const provinceStyle={color:'#6b5b95',weight:1.5,dashArray:'4 3',fill:false,opacity:.9};
export const provinceTipLines=(p:any):string[]=>[String(p?.name||p?.iso||'')].filter(Boolean);
export const trialStyle=(onA:boolean)=>({color:'#1f4fd1',weight:5,opacity:.95,dashArray:onA?undefined:'4 4'});

// White everywhere but Vietnam: a world ring with the national boundary (or the land ring) cut out.
const world=[[-180,-85],[180,-85],[180,85],[-180,85],[-180,-85]];
export const maskStyle={stroke:false,fillColor:'#ffffff',fillOpacity:1};
// Every outer ring becomes a hole, so a MultiPolygon boundary (mainland plus offshore islands, spec 18.15) shows them all.
export function landMask(land:Feature):Feature<Polygon>{const g=land.geometry;const rings=g.type==='Polygon'?[g.coordinates[0]||[]]:g.type==='MultiPolygon'?g.coordinates.map(poly=>poly[0]).filter(Boolean):[[]];return{type:'Feature',properties:{},geometry:{type:'Polygon',coordinates:[world,...rings]}};}
export function maskFor(boundary:Feature|null|undefined,land:Feature|null|undefined):Feature<Polygon>|null{const hole=boundary||land;return hole?landMask(hole):null;}

const deadEndReason:Record<string,string>={construction:'Đường đang xây',continues:'Đường chạy tiếp nhưng OSRM không nối được trong 2 km',end:'Hết đường trong OSM'};
export const roadTipLines=(p:any):string[]=>(p.bridge_kind==='ferry'?[...ferryTipLines(p),p.isolated?'Khúc bị cô lập':'']:[[p.name,p.ref].filter(Boolean).join(' · ')||`(${p.highway})`,`${p.highway} · ${Number(p.km).toLocaleString('vi-VN',{maximumFractionDigits:2})} km`,p.bridge?p.bridge_kind==='small'?'Nối bù qua đường nhỏ (theo OSRM)':'Nối bù (OSM thiếu số hiệu)':p.ref_only?'Chỉ giữ nhờ số hiệu':'',p.isolated?'Khúc bị cô lập':'']).filter(Boolean);
export const junctionTipLines=(p:any):string[]=>(p.type==='border'?[p.crossing,p.big_road,'Giao biên giới']:p.type==='deadend'?[p.big_road,'Điểm cụt',deadEndReason[p.reason]||'']:[p.big_road,`Giao ${p.type}: ${(p.joins||[]).join(', ')}`]).filter(Boolean);
const roadTip=(p:any)=>tip(roadTipLines(p)),junctionTip=(p:any)=>tip(junctionTipLines(p));

// Below zoom 8 the border squares and dead-end rings shrink so they do not drown the road colours.
export function junctionMarkerOptions(type:string,compact:boolean):any{
 if(type==='border'){const size=compact?5:7;return{iconSize:[size,size]};}
 if(type==='deadend')return{radius:compact?3:4,color:'#222222',weight:1.5,fill:false};
 return{radius:3,weight:1,color:'#ffffff',fillColor:type==='B'?'#3974d8':'#2a9d8f',fillOpacity:.95};
}
const borderIcons={compact:L.divIcon({className:'border-point',...junctionMarkerOptions('border',true)}),normal:L.divIcon({className:'border-point',...junctionMarkerOptions('border',false)})};
// Border crossings are DOM markers so they sit above the canvas roads and stay square.
const junctionPoint=(compact:boolean)=>(feature:Feature<Geometry,any>,latlng:L.LatLng)=>{const type=feature.properties?.type;return type==='border'?L.marker(latlng,{icon:compact?borderIcons.compact:borderIcons.normal}):L.circleMarker(latlng,junctionMarkerOptions(type,compact));};
function boundsOf(features:Feature[]){let south=90,west=180,north=-90,east=-180;for(const feature of features){const g=feature.geometry;const coords=g.type==='LineString'?g.coordinates:g.type==='Polygon'?g.coordinates[0]:[];for(const [lon,lat] of coords){south=Math.min(south,lat);north=Math.max(north,lat);west=Math.min(west,lon);east=Math.max(east,lon);}}return south<=north?L.latLngBounds([south,west],[north,east]):null;}
const toLatLon=(e:L.LeafletMouseEvent):LatLon=>[Number(e.latlng.lat.toFixed(6)),Number(e.latlng.lng.toFixed(6))];

function useLatest<T>(value:T){const ref=useRef(value);ref.current=value;return ref;}

function BigRoadLayers({view,onRoadClick,onJunctionClick}:{view:BigRoadView}&Pick<Props,'onRoadClick'|'onJunctionClick'>){
 const map=useMap();
 const mask=useMemo(()=>maskFor(view.boundary,view.land),[view.boundary,view.land]);
 const roadClick=useLatest(onRoadClick),junctionClick=useLatest(onJunctionClick);
 // Fit the whole network each time the mode is entered; later filtering must not move the map.
 // Leaflet caches the container size and the layout changes in the same render, so refresh it first.
 useEffect(()=>{const land=view.land;const bounds=boundsOf(land?[land]:view.roads.features);if(!bounds)return;map.invalidateSize({pan:false});map.fitBounds(bounds,{padding:[24,24],animate:false});if(!land)return;map.options.maxBoundsViscosity=1;map.setMaxBounds(bounds.pad(0.05));map.setMinZoom(map.getBoundsZoom(bounds));return()=>{map.setMaxBounds(null as unknown as L.LatLngBounds);map.setMinZoom(0);};// eslint-disable-next-line react-hooks/exhaustive-deps
 },[view.land,map]);
 const style=useMemo(()=>makeRoadStyle({colorBy:view.colorBy,clusters:view.clusters,clusterColors:view.clusterColors,highlight:view.highlight}),[view.colorBy,view.clusters,view.clusterColors,view.highlight]);
 return <>
  {mask&&<GeoJSON key="mask" data={mask} style={maskStyle} interactive={false}/>}
  {view.land&&<GeoJSON key="land-outline" data={view.land} style={{color:'#c3cec9',weight:1,fill:false}} interactive={false}/>}
  {view.provinces&&<GeoJSON key="provinces" data={view.provinces} style={provinceStyle} onEachFeature={(feature,layer)=>layer.bindTooltip(()=>tip(provinceTipLines(feature.properties)),{sticky:true})}/>}
  <GeoJSON key={`big-roads-${view.roadKey??''}`} data={view.roads} style={style} onEachFeature={(feature,layer)=>{layer.bindTooltip(()=>roadTip(feature.properties),{sticky:true});layer.on('click',(e:L.LeafletMouseEvent)=>{L.DomEvent.stopPropagation(e);roadClick.current?.(feature,toLatLon(e));});}}/>
  <GeoJSON key={`junctions-${view.junctionKey}`} data={view.junctions} pointToLayer={junctionPoint(!!view.compact)} onEachFeature={(feature,layer)=>{layer.bindTooltip(()=>junctionTip(feature.properties));layer.on('click',(e:L.LeafletMouseEvent)=>{L.DomEvent.stopPropagation(e);junctionClick.current?.(feature,toLatLon(e));});}}/>
  {view.cuts&&<CutLayer key={`cuts-${view.cutKey??''}`} cuts={view.cuts} nodes={view.boundaryNodes||[]}/>}
  <FocusLayer focus={view.focus}/>
  <TrialLayer trial={view.trial} points={view.trialPoints}/>
 </>;
}
// Fly to a review item and pulse it for about two seconds, or fit a selected route.
function FocusLayer({focus}:{focus?:MapFocus|null}){
 const map=useMap();const [pulse,setPulse]=useState<LatLon|null>(null);
 useEffect(()=>{
  if(!focus)return;
  if(focus.kind==='bounds'){const [w,s,e,n]=focus.bbox;try{map.invalidateSize({pan:false});map.fitBounds([[s,w],[n,e]],{padding:[40,40],maxZoom:14});}catch{/* map not sized yet */}return;}
  const [lon,lat]=focus.at;setPulse([lat,lon]);
  try{map.invalidateSize({pan:false});map.flyTo([lat,lon],15,{duration:.8});}catch{try{map.setView([lat,lon],15);}catch{/* map not sized yet */}}
  const timer=setTimeout(()=>setPulse(null),2000);return()=>clearTimeout(timer);
 },[focus?.seq,map]);// eslint-disable-line react-hooks/exhaustive-deps
 return pulse?<Marker position={pulse} icon={pulseIcon} interactive={false}/>:null;
}
function CutLayer({cuts,nodes}:{cuts:CutInfo[];nodes:number[][]}){
 const lines=cuts.map(c=>c.coords.map(([lon,lat])=>[lat,lon] as LatLon));
 return <>
  {lines.map((line,i)=><Polyline key={`casing-${i}`} positions={line} pathOptions={cutCasingStyle} interactive={false}/>)}
  {lines.map((line,i)=><Polyline key={`cut-${i}`} positions={line} pathOptions={cutStyle}><Tooltip sticky>{cutTipText(cuts[i])}</Tooltip></Polyline>)}
  {nodes.map(([lon,lat],i)=><CircleMarker key={`node-${i}`} center={[lat,lon]} pathOptions={boundaryNodeStyle} radius={boundaryNodeStyle.radius} interactive={false}/>)}
 </>;
}
function TrialLayer({trial,points}:{trial?:TrialRoute|null;points?:TrialPoints|null}){
 return <>
  {trial?.segments.map((segment,index)=><Polyline key={index} positions={segment.coords.map(([lon,lat])=>[lat,lon] as LatLon)} pathOptions={trialStyle(segment.on_a)} interactive={false}/>)}
  {points?.origin&&<Marker position={points.origin} icon={aIcon}><Tooltip>Điểm đi thử</Tooltip></Marker>}
  {points?.destination&&<Marker position={points.destination} icon={bIcon}><Tooltip>Điểm đến thử</Tooltip></Marker>}
 </>;
}
// Buildings share the map's canvas renderer (a second canvas on top would swallow the clicks meant for the roads) and
// are moved to the back of its draw list, under the roads. Polygons are kept between redraws and only the difference
// is added or removed. Downloads still wanted after a move go on; the others are aborted.
export const buildingStyle=(fill:string)=>({color:'#173e4c',weight:.5,opacity:.55,fillColor:fill,fillOpacity:.85});
function BuildingLayer({onStatus}:{onStatus?:(status:BuildingStatus)=>void}){
 const map=useMap();const status=useLatest(onStatus);
 useEffect(()=>{
  const group=L.layerGroup().addTo(map),drawn=new Map<string,L.Polygon>(),inflight=new Map<string,{ctrl:AbortController;promise:Promise<BuildingFile>}>();
  let seq=0,disposed=false;
  const report=(s:BuildingStatus)=>{if(!disposed)status.current?.(s);};
  const draw=(entries:IndexEntry[],view:BBox,area:BBox)=>{
   const files=entries.map(e=>buildingStore.cached(e.file)).filter((f):f is BuildingFile=>!!f),{items,total}=selectBuildings(files,area,view),keep=new Set<string>();
   for(const {file,i} of items){
    const key=`${file.name}:${i}`;keep.add(key);if(drawn.has(key))continue;
    const fp=footprintOf(file,i),poly=L.polygon([fp.outer,...fp.holes].map(r=>r.map(([lon,lat])=>[lat,lon] as LatLon)),buildingStyle(floorColor(floorsOf(file,i))));
    poly.on('click',(e:L.LeafletMouseEvent)=>{if(map.getContainer().classList.contains('picking'))return;L.DomEvent.stopPropagation(e);L.popup().setLatLng(e.latlng).setContent(tip(buildingLines(file,i))).openOn(map);});
    group.addLayer(poly);poly.bringToBack();drawn.set(key,poly);
   }
   for(const [key,poly] of drawn)if(!keep.has(key)){group.removeLayer(poly);drawn.delete(key);}
   return{shown:items.length,total};
  };
  const stopAll=()=>{for(const x of inflight.values())x.ctrl.abort();inflight.clear();};
  async function update(){
   const my=++seq,current=()=>my===seq&&!disposed;
   if(!buildingsShown(map.getZoom())){stopAll();group.clearLayers();drawn.clear();report({kind:'zoom'});return;}
   const b=map.getBounds(),view:BBox=[b.getWest(),b.getSouth(),b.getEast(),b.getNorth()],area=padBBox(view,VIEW_MARGIN);
   let shown=0;
   try{
    const index=await buildingStore.index();if(!current())return;
    const entries=buildingStore.needed(index,area),wanted=new Set(entries.map(e=>e.file));
    for(const [name,x] of inflight)if(!wanted.has(name)){x.ctrl.abort();inflight.delete(name);}
    shown=draw(entries,view,area).shown;
    for(const e of entries){
     if(buildingStore.cached(e.file))continue;
     const loading=(network:boolean)=>{if(current())report({kind:'loading',file:e.file,bytes:e.bytes,network,shown});};
     let x=inflight.get(e.file);
     if(!x){const ctrl=new AbortController(),job={ctrl,promise:buildingStore.file(e,ctrl.signal,()=>loading(true))};x=job;inflight.set(e.file,job);const done=()=>{if(inflight.get(e.file)===job)inflight.delete(e.file);};job.promise.then(done,done);}
     loading(false);
     await x.promise;if(!current())return;
     shown=draw(buildingStore.needed(index,area),view,area).shown;
    }
    const r=draw(buildingStore.needed(index,area),view,area);report({kind:'ready',shown:r.shown,total:r.total,offline:index.offline});
   }catch(e){if(!current()||(e as Error)?.name==='AbortError')return;report({kind:'error',message:e instanceof BuildingError?e.message:'Không vẽ được lớp nhà.',shown});}
  }
  const onMove=()=>{void update();};
  map.on('moveend',onMove);onMove();
  return()=>{disposed=true;map.off('moveend',onMove);stopAll();group.remove();};
 },[map]);// eslint-disable-line react-hooks/exhaustive-deps
 return null;
}
// Rendered after the big-road layers so its effect reports the zoom after they fit the map (spec 18.2, L1).
function ZoomReporter({onZoomChange,mode}:{onZoomChange:(zoom:number)=>void;mode:string}){const map=useMapEvents({zoomend(){onZoomChange(map.getZoom());}});useEffect(()=>{onZoomChange(map.getZoom());},[map,onZoomChange,mode]);return null;}
// MapContainer applies className only once, so state-driven classes are toggled on the live container.
function ContainerClasses({picking,noBasemap}:{picking:boolean;noBasemap:boolean}){const map=useMap();useEffect(()=>{const el=map.getContainer();el.classList.toggle('picking',picking);el.classList.toggle('no-basemap',noBasemap);},[map,picking,noBasemap]);return null;}
// The map box changes size when the drawer opens or closes.
function SizeWatcher(){const map=useMap();useEffect(()=>{if(typeof ResizeObserver==='undefined')return;const observer=new ResizeObserver(()=>map.invalidateSize());observer.observe(map.getContainer());return()=>observer.disconnect();},[map]);return null;}
function LayoutWatcher({layoutKey}:{layoutKey?:string}){const map=useMap();const first=useRef(true);useEffect(()=>{if(first.current){first.current=false;return;}map.invalidateSize({pan:false});},[map,layoutKey]);return null;}
function MapEvents({picking,onPick,route,bigActive,onMapClick}:Pick<Props,'picking'|'onPick'|'route'|'onMapClick'>&{bigActive:boolean}){
 const map=useMap();const mapClick=useLatest(onMapClick);
 useMapEvents({click(event){if(picking){onPick({lat:Number(event.latlng.lat.toFixed(6)),lon:Number(event.latlng.lng.toFixed(6))});return;}if(bigActive)mapClick.current?.(toLatLon(event));}});
 useEffect(()=>{if(!bigActive&&route?.geometry.coordinates.length){const positions=route.geometry.coordinates.map(([lon,lat])=>[lat,lon] as LatLon);map.fitBounds(positions,{padding:[56,56],maxZoom:15});}},[route,map,bigActive]);
 return null;
}
export default function TrafficMap(props:Props){
 const {origin,destination,route,classes,picking,onPick,bigRoads,basemap=true,onZoomChange,onRoadClick,onJunctionClick,onMapClick,layoutKey,buildings=false,onBuildingStatus}=props;
 // A few pixels of click tolerance so thin canvas roads can be clicked for their details (spec 18.7).
 const renderer=useMemo(()=>L.canvas({tolerance:5}),[]);
 return <MapContainer center={[21.05,105.83]} zoom={11} zoomControl={false} preferCanvas renderer={renderer} className="map">
  <ContainerClasses picking={!!picking} noBasemap={!basemap}/><SizeWatcher/><LayoutWatcher layoutKey={layoutKey}/><ZoomControl position="topleft"/>
  {basemap&&<TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' url="https://tile.openstreetmap.org/{z}/{x}/{y}.png" maxZoom={19}/>}
  <MapEvents picking={picking} onPick={onPick} route={route} bigActive={!!bigRoads} onMapClick={onMapClick}/>
  {bigRoads?<BigRoadLayers view={bigRoads} onRoadClick={onRoadClick} onJunctionClick={onJunctionClick}/>:<>
   <Marker position={[origin.lat,origin.lon]} icon={aIcon}><Tooltip>Điểm đi</Tooltip></Marker>
   <Marker position={[destination.lat,destination.lon]} icon={bIcon}><Tooltip>Điểm đến</Tooltip></Marker>
   {route?.segments?.length?route.segments.map((segment,index)=><Polyline key={index} positions={segment.geometry.coordinates.map(([lon,lat])=>[lat,lon] as LatLon)} pathOptions={{color:classes.find(c=>c.id===segment.road_class)?.color||'#0f8e8e',weight:5,opacity:.92}}><Tooltip>{classes.find(c=>c.id===segment.road_class)?.label||segment.road_class} · {segment.speed_kmh.toFixed(1)} km/h</Tooltip></Polyline>):route&&<Polyline positions={route.geometry.coordinates.map(([lon,lat])=>[lat,lon] as LatLon)} pathOptions={{color:'#0e8e89',weight:5}}/>}
  </>}
  {buildings&&<BuildingLayer onStatus={onBuildingStatus}/>}
  {onZoomChange&&<ZoomReporter onZoomChange={onZoomChange} mode={bigRoads?'big':'route'}/>}
 </MapContainer>;
}
