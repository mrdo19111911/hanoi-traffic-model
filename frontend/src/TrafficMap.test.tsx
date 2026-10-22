import {render} from '@testing-library/react';
import L from 'leaflet';
import {describe,it,expect,vi} from 'vitest';
import {act} from '@testing-library/react';
import TrafficMap,{provinceStyle,provinceTipLines,boundaryNodeStyle,cutCasingStyle,cutStyle,junctionMarkerOptions,junctionTipLines,landMask,makeRoadStyle,maskFor,maskStyle,roadStyle,roadTipLines,trialStyle} from './TrafficMap';
const base={origin:{lat:21.03,lon:105.85},destination:{lat:21.2,lon:105.8},route:null,classes:[],onPick:()=>{}};
describe('TrafficMap container state',()=>{
 it('updates the basemap and picking classes after the map is created',()=>{
  const {container,rerender}=render(<TrafficMap {...base} picking={null} basemap={true}/>);
  const map=container.querySelector('.leaflet-container')!;
  expect(map.classList.contains('no-basemap')).toBe(false);expect(map.classList.contains('picking')).toBe(false);
  rerender(<TrafficMap {...base} picking="origin" basemap={false}/>);
  expect(map.classList.contains('no-basemap')).toBe(true);expect(map.classList.contains('picking')).toBe(true);
  rerender(<TrafficMap {...base} picking={null} basemap={true}/>);
  expect(map.classList.contains('no-basemap')).toBe(false);expect(map.classList.contains('picking')).toBe(false);
 });
 it('loads no basemap tiles while the basemap is off',()=>{
  const {container}=render(<TrafficMap {...base} picking={null} basemap={false}/>);
  expect(container.querySelector('.leaflet-tile-pane img')).toBeNull();
 });
 it('shows the Leaflet zoom buttons',()=>{
  const {container}=render(<TrafficMap {...base} picking={null}/>);
  expect(container.querySelector('.leaflet-control-zoom')).not.toBeNull();
 });
 it('refreshes the map size when the layout around it changes (spec 20)',()=>{
  const spy=vi.spyOn(L.Map.prototype,'invalidateSize');
  const {rerender}=render(<TrafficMap {...base} picking={null} layoutKey="open"/>);
  const before=spy.mock.calls.length;
  rerender(<TrafficMap {...base} picking={null} layoutKey="closed"/>);
  expect(spy.mock.calls.length).toBeGreaterThan(before);
 });
});
describe('big-road drawing rules',()=>{
 const road=(properties:any)=>({type:'Feature',geometry:{type:'LineString',coordinates:[]},properties}) as any;
 it('masks everything outside the mainland in solid white',()=>{
  const ring=[[102,8],[110,8],[110,23.5],[102,8]];
  const mask=landMask({type:'Feature',geometry:{type:'Polygon',coordinates:[ring]},properties:{}} as any);
  expect(mask.geometry.type).toBe('Polygon');
  expect(mask.geometry.coordinates).toHaveLength(2);
  expect(mask.geometry.coordinates[0]).toEqual([[-180,-85],[180,-85],[180,85],[-180,85],[-180,-85]]);
  expect(mask.geometry.coordinates[1]).toEqual(ring);
  expect(maskStyle).toMatchObject({fillColor:'#ffffff',fillOpacity:1,stroke:false});
 });
 it('draws provincial roads solid and yellow, and bridges dotted in their group colour',()=>{
  expect(roadStyle(road({group:'tinhlo',ref_only:true}))).toMatchObject({color:'#d4b12f',weight:1.8,dashArray:undefined});
  expect(roadStyle(road({group:'quoclo',ref_only:true})).dashArray).toBe('6 4');
  expect(roadStyle(road({group:'quoclo',bridge:true}))).toMatchObject({color:'#e89b36',dashArray:'2 3'});
  expect(roadStyle(road({group:'tinhlo',isolated:true})).color).toBe('#8a949a');
 });
 it('draws ferries as dashed blue lines, fainter when OSM does not confirm cars',()=>{
  const ferry={group:'quoclo',bridge:true,bridge_kind:'ferry',car_confirmed:true};
  expect(roadStyle(road(ferry))).toMatchObject({color:'#2f6fb5',weight:4,dashArray:'6 4',opacity:.9});
  expect(roadStyle(road({...ferry,car_confirmed:false}))).toMatchObject({color:'#2f6fb5',weight:4,dashArray:'6 4',opacity:.55});
  expect(roadStyle(road({...ferry,isolated:true})).color).toBe('#8a949a');
  expect(roadStyle(road({group:'quoclo',bridge:true,bridge_kind:'small'}))).toMatchObject({color:'#e89b36',dashArray:'2 3'});
 });
 it('describes ferries and small-road bridges in the road tooltip',()=>{
  expect(roadTipLines({bridge:true,bridge_kind:'ferry',ferry_name:'Phà Đại Ngãi',car_confirmed:true,ferry_min:20,ferry_min_source:'osm',km:2.1})).toEqual(['Phà Đại Ngãi','khoảng 20 phút','theo OSM']);
  expect(roadTipLines({bridge:true,bridge_kind:'ferry',ferry_name:'',car_confirmed:false,ferry_min:36,ferry_min_source:'osrm_5kmh',km:3})).toEqual(['Phà','khoảng 36 phút','OSRM ước tính 5 km/h','OSM chưa ghi chở ô tô']);
  const small=roadTipLines({name:'Ngõ 1',ref:'',highway:'residential',km:0.3,bridge:true,bridge_kind:'small'});
  expect(small).toContain('Nối bù qua đường nhỏ (theo OSRM)');expect(small).not.toContain('Nối bù (OSM thiếu số hiệu)');
  expect(roadTipLines({name:'Phố Huyện',ref:'',highway:'primary',km:0.3,bridge:true,bridge_kind:'road'})).toContain('Nối bù (OSM thiếu số hiệu)');
 });
 it('explains why a dead end stops in its tooltip',()=>{
  expect(junctionTipLines({type:'deadend',big_road:'CT.41',reason:'construction'})).toEqual(['CT.41','Điểm cụt','Đường đang xây']);
  expect(junctionTipLines({type:'deadend',big_road:'QL.57',reason:'continues'})).toEqual(['QL.57','Điểm cụt','Đường chạy tiếp nhưng OSRM không nối được trong 2 km']);
  expect(junctionTipLines({type:'deadend',big_road:'ĐT.70',reason:'end'})).toEqual(['ĐT.70','Điểm cụt','Hết đường trong OSM']);
  expect(junctionTipLines({type:'deadend',big_road:'QL.1'})).toEqual(['QL.1','Điểm cụt']);
 });
});
describe('big-road display density and mask',()=>{
 const ring=(x:number)=>[[x,8],[110,8],[110,23.5],[x,8]];
 const poly=(x:number)=>({type:'Feature',geometry:{type:'Polygon',coordinates:[ring(x)]},properties:{}}) as any;
 it('cuts the mask hole along the national boundary when it is available',()=>{
  expect(maskFor(poly(101),poly(102))!.geometry.coordinates[1]).toEqual(ring(101));
  expect(maskFor(null,poly(102))!.geometry.coordinates[1]).toEqual(ring(102));
  expect(maskFor(undefined,poly(102))!.geometry.coordinates[0]).toEqual([[-180,-85],[180,-85],[180,85],[-180,85],[-180,-85]]);
  expect(maskFor(null,null)).toBeNull();
 });
 it('cuts every polygon of a MultiPolygon boundary out of the mask',()=>{
  const multi={type:'Feature',geometry:{type:'MultiPolygon',coordinates:[[ring(101)],[ring(111)],[ring(107)]]},properties:{}} as any;
  const mask=maskFor(multi,poly(102))!;
  expect(mask.geometry.coordinates).toEqual([[[-180,-85],[180,-85],[180,85],[-180,85],[-180,-85]],ring(101),ring(111),ring(107)]);
 });
 it('shrinks border squares and dead-end rings below zoom 8',()=>{
  expect(junctionMarkerOptions('border',true)).toMatchObject({iconSize:[5,5]});
  expect(junctionMarkerOptions('border',false)).toMatchObject({iconSize:[7,7]});
  expect(junctionMarkerOptions('deadend',true)).toMatchObject({radius:3,color:'#222222',weight:1.5,fill:false});
  expect(junctionMarkerOptions('deadend',false)).toMatchObject({radius:4});
  expect(junctionMarkerOptions('B',false)).toMatchObject({radius:3,fillColor:'#3974d8'});
  expect(junctionMarkerOptions('C',true)).toMatchObject({radius:3,fillColor:'#2a9d8f'});
 });
});
describe('big-road colouring and selection',()=>{
 const road=(properties:any)=>({type:'Feature',geometry:{type:'LineString',coordinates:[]},properties}) as any;
 it('colours by free-flow speed while keeping dashes and widths',()=>{
  const style=makeRoadStyle({colorBy:'speed'});
  expect(style(road({group:'quoclo',speed_kmh:29.9}))).toMatchObject({color:'#c0392b',weight:2.5});
  expect(style(road({group:'caotoc',speed_kmh:90,ref_only:true}))).toMatchObject({color:'#1e8449',weight:3,dashArray:'6 4'});
  expect(style(road({group:'quoclo',bridge:true,bridge_kind:'ferry',speed_kmh:5,car_confirmed:true}))).toMatchObject({color:'#c0392b',weight:4,dashArray:'6 4'});
  expect(style(road({group:'tinhlo',speed_kmh:null}))).toMatchObject({color:'#9aa5ab',weight:1.8});
 });
 it('colours by cluster when the cluster layer is on',()=>{
  const style=makeRoadStyle({colorBy:'cluster',clusters:{'1':0,'2':13},clusterColors:{0:'#2ca02c',13:'#d62728'}});
  expect(style(road({way_id:1,group:'quoclo'})).color).toBe('#2ca02c');
  expect(style(road({way_id:2,group:'quoclo'})).color).toBe('#d62728');
  expect(style(road({way_id:3,group:'quoclo'})).color).toBe('#9aa5ab');
 });
 it('thickens the selected route twofold and dims the others to 0.25',()=>{
  const chosen=road({group:'quoclo'}),other=road({group:'caotoc'});
  const style=makeRoadStyle({colorBy:'group',highlight:new Set([chosen])});
  expect(style(chosen)).toMatchObject({weight:5,opacity:.9});
  expect(style(other)).toMatchObject({weight:3,opacity:.25});
  expect(makeRoadStyle({colorBy:'group'})(other)).toMatchObject({weight:3,opacity:.9});
 });
 it('draws trial route segments solid on network A and dashed off it',()=>{
  expect(trialStyle(true)).toEqual({color:'#1f4fd1',weight:5,opacity:.95,dashArray:undefined});
  expect(trialStyle(false)).toEqual({color:'#1f4fd1',weight:5,opacity:.95,dashArray:'4 4'});
 });
});
describe('cluster cut drawing',()=>{
 it('draws cuts black and 4 wide over a white casing, and boundary nodes as black dots',()=>{
  expect(cutStyle).toMatchObject({color:'#000000',weight:4,opacity:1});
  expect(cutCasingStyle).toMatchObject({color:'#ffffff',opacity:1});
  expect(cutCasingStyle.weight).toBeGreaterThan(cutStyle.weight);
  expect(boundaryNodeStyle).toMatchObject({radius:3,color:'#000000',fillColor:'#000000',fillOpacity:1});
 });
});
describe('province boundaries (19.6)',()=>{
 it('draws province outlines purple, dashed and unfilled',()=>{
  expect(provinceStyle).toEqual({color:'#6b5b95',weight:1.5,dashArray:'4 3',fill:false,opacity:.9});
 });
 it('shows the province name on hover',()=>{
  expect(provinceTipLines({name:'Thành phố Hà Nội',iso:'VN-HN'})).toEqual(['Thành phố Hà Nội']);
  expect(provinceTipLines({iso:'VN-49'})).toEqual(['VN-49']);
 });
});
describe('TrafficMap big-road layers',()=>{
 const view={roads:{type:'FeatureCollection',features:[]},junctions:{type:'FeatureCollection',features:[{type:'Feature',geometry:{type:'Point',coordinates:[106.7116,21.9737]},properties:{node_id:null,type:'border',joins:[],big_road:'Đường Hữu Nghị (QL.1)',crossing:'Cửa khẩu Hữu Nghị',island:false}}]},junctionKey:'k',land:null} as any;
 it('draws border crossings as square markers',()=>{
  const {container}=render(<TrafficMap {...base} picking={null} bigRoads={view}/>);
  expect(container.querySelectorAll('.border-point')).toHaveLength(1);
 });
 it('reports the zoom level to its parent',()=>{
  const onZoomChange=vi.fn();render(<TrafficMap {...base} picking={null} onZoomChange={onZoomChange}/>);
  expect(onZoomChange).toHaveBeenCalledWith(11);
 });
 it('refreshes the cached map size before fitting the network or a focused route',()=>{
  const calls:string[]=[];
  // jsdom has no canvas: a context whose every method is a no-op lets the mask layer draw.
  const ctx=new Proxy({},{get:(target:any,key)=>key in target?target[key]:()=>{}});
  const getContext=vi.spyOn(HTMLCanvasElement.prototype,'getContext').mockReturnValue(ctx);
  const invalidate=vi.spyOn(L.Map.prototype,'invalidateSize').mockImplementation(function(this:any){calls.push('invalidate');return this;});
  const fit=vi.spyOn(L.Map.prototype,'fitBounds').mockImplementation(function(this:any){calls.push('fit');return this;});
  const land={type:'Feature',geometry:{type:'Polygon',coordinates:[[[102,8],[110,8],[110,23.5],[102,8]]]},properties:{}};
  const {rerender}=render(<TrafficMap {...base} picking={null} bigRoads={{...view,land}}/>);
  expect(calls.slice(0,2)).toEqual(['invalidate','fit']);
  calls.length=0;
  rerender(<TrafficMap {...base} picking={null} bigRoads={{...view,land,focus:{seq:2,kind:'bounds',bbox:[105.8,20.8,106.7,21.1]}}}/>);
  expect(calls).toEqual(['invalidate','fit']);
  invalidate.mockRestore();fit.mockRestore();getContext.mockRestore();
 });
 it('pulses a focused point for about two seconds',()=>{
  vi.useFakeTimers();
  try{
   const {container}=render(<TrafficMap {...base} picking={null} bigRoads={{...view,focus:{seq:1,kind:'point',at:[106,21]}}}/>);
   expect(container.querySelectorAll('.pulse-point')).toHaveLength(1);
   act(()=>{vi.advanceTimersByTime(2100);});
   expect(container.querySelectorAll('.pulse-point')).toHaveLength(0);
  }finally{vi.useRealTimers();}
 });
});
