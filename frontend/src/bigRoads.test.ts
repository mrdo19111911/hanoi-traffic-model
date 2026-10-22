import {describe,it,expect} from 'vitest';
import {normalizeRef,refMatches,foldText,searchRoutes,routeStats,lineKm,speedColor,SPEED_BANDS,clusterColor,CLUSTER_COLORS,reviewItems,filterReview,reviewCounts,reviewCsv,osmViewUrl,osmEditUrl,visibleGroups,pickPartition,clusterAdjacency,partitionAdjacency,colorClusters,cutInfo,cutTipText,clusterStatRows,clustersOutdated,provincePartition,naturalPartition,partitionLabel} from './bigRoads';

const road=(id:number,props:any,coordinates:number[][]=[[105,21],[105.01,21]])=>({type:'Feature',geometry:{type:'LineString',coordinates},properties:{way_id:id,group:'quoclo',highway:'trunk',name:'',ref:'',km:1,...props}}) as any;
const point=(props:any,coordinates:number[])=>({type:'Feature',geometry:{type:'Point',coordinates},properties:props}) as any;

describe('route ref normalisation',()=>{
 it('drops dots and spaces and ignores case',()=>{
  expect(normalizeRef('QL.1')).toBe('QL1');
  expect(normalizeRef(' ql 1 ')).toBe('QL1');
  expect(normalizeRef('CT.01')).toBe('CT01');
 });
 it('treats DT and TL as ĐT',()=>{
  expect(normalizeRef('dt915')).toBe('ĐT915');
  expect(normalizeRef('tl915')).toBe('ĐT915');
  expect(normalizeRef('ĐT.915')).toBe('ĐT915');
  expect(normalizeRef('đt 915')).toBe('ĐT915');
 });
 it('matches one ref of a semicolon list exactly',()=>{
  expect(refMatches('QL.1','ql1')).toBe(true);
  expect(refMatches('QL.1A','ql1')).toBe(false);
  expect(refMatches('ĐT.915','dt915')).toBe(true);
  expect(refMatches('ĐT.915','tl915')).toBe(true);
  expect(refMatches('QL.21;QL.1','ql1')).toBe(true);
  expect(refMatches('QL.21; QL.1A','ql1')).toBe(false);
  expect(refMatches('','ql1')).toBe(false);
 });
 it('folds case and Vietnamese diacritics for names',()=>{
  expect(foldText('Đường Hồ Chí Minh')).toBe('duong ho chi minh');
  expect(foldText('NGỌC HỒI')).toBe('ngoc hoi');
 });
});

describe('route search',()=>{
 const features=[road(1,{ref:'QL.1',name:'Ngọc Hồi',km:10}),road(2,{ref:'QL.1',name:'Giải Phóng',km:5}),road(3,{ref:'QL.1A',name:'',km:7}),road(4,{ref:'ĐT.915',name:'',group:'tinhlo',km:3}),road(5,{ref:'',name:'Đại lộ Thăng Long',km:20}),road(6,{ref:'QL.21;QL.1',name:'',km:2})];
 it('finds a route by normalised ref with its total km',()=>{
  const hits=searchRoutes(features,'ql1');
  expect(hits[0]).toMatchObject({label:'QL.1',km:17});
  expect(hits.map(h=>h.label)).not.toContain('QL.1A');
  expect(searchRoutes(features,'tl915')[0]).toMatchObject({label:'ĐT.915',km:3});
 });
 it('finds a route by a case and diacritic insensitive name substring',()=>{
  expect(searchRoutes(features,'thang long').map(h=>h.label)).toEqual(['Đại lộ Thăng Long']);
  expect(searchRoutes(features,'GIAI PHONG').map(h=>h.label)).toEqual(['QL.1']);
 });
 it('returns at most ten suggestions and nothing for a blank query',()=>{
  const many=Array.from({length:15},(_,i)=>road(100+i,{ref:`QL.${i+2}`,name:'Quốc lộ',km:i}));
  expect(searchRoutes(many,'quoc lo')).toHaveLength(10);
  expect(searchRoutes(many,'  ')).toEqual([]);
 });
 it('selects every feature of the route and counts pieces and dead ends on it',()=>{
  const hit=searchRoutes(features,'ql1')[0];
  const parts=[road(1,{ref:'QL.1',km:10},[[105,21],[105.1,21]]),road(2,{ref:'QL.1',km:5},[[105.1,21],[105.2,21]]),road(3,{ref:'QL.1',km:4},[[106,21],[106.1,21]]),road(4,{ref:'QL.1A',km:9},[[105.2,21],[105.3,21]])];
  const dead=[point({type:'deadend',node_id:1},[106.1,21]),point({type:'deadend',node_id:2},[105.3,21]),point({type:'B',node_id:3},[105,21])];
  const stats=routeStats(parts,dead,hit);
  expect(stats.features.map((f:any)=>f.properties.way_id)).toEqual([1,2,3]);
  expect(stats.km).toBe(19);expect(stats.pieces).toBe(2);expect(stats.deadEnds).toBe(1);
  expect(stats.bbox).toEqual([105,21,106.1,21]);
 });
});

describe('segment length',()=>{
 it('measures a line with the haversine formula',()=>{
  expect(lineKm([[105,21],[105,22]])).toBeCloseTo(111.19,1);
  expect(lineKm([[105,21]])).toBe(0);
 });
});

describe('speed colour scale',()=>{
 it('uses five bands with exact thresholds',()=>{
  expect(speedColor(29.9)).toBe('#c0392b');
  expect(speedColor(30)).toBe('#e67e22');
  expect(speedColor(49.9)).toBe('#e67e22');
  expect(speedColor(50)).toBe('#f1c40f');
  expect(speedColor(70)).toBe('#7fb800');
  expect(speedColor(89.9)).toBe('#7fb800');
  expect(speedColor(90)).toBe('#1e8449');
  expect(speedColor(130)).toBe('#1e8449');
  expect(SPEED_BANDS.map(b=>b.color)).toEqual(['#c0392b','#e67e22','#f1c40f','#7fb800','#1e8449']);
 });
 it('greys out unknown speeds',()=>{
  expect(speedColor(null)).toBe('#9aa5ab');
  expect(speedColor(undefined)).toBe('#9aa5ab');
 });
});

describe('cluster colours',()=>{
 it('cycles through twelve colours',()=>{
  expect(CLUSTER_COLORS).toHaveLength(12);
  expect(new Set(CLUSTER_COLORS).size).toBe(12);
  expect(clusterColor(0)).toBe(CLUSTER_COLORS[0]);
  expect(clusterColor(13)).toBe(CLUSTER_COLORS[1]);
  expect(clusterColor(undefined)).toBe('#9aa5ab');
 });
});

describe('cluster drawing (18.14)',()=>{
 // Cluster 0 on the left, 1 in the middle, 2 on the right; 0 and 2 never touch.
 const roads=[road(1,{},[[0,0],[1,0]]),road(4,{},[[1,0],[1,1]]),road(3,{},[[1,0],[2,0]]),road(2,{},[[2,0],[3,0]]),road(5,{},[[3,0],[4,0]]),road(6,{},[[4,0],[5,0]]),road(9,{},[[9,9],[9,8]])];
 const ways={'1':0,'4':0,'3':1,'2':1,'5':1,'6':2,'9':3};
 const partition=(k:number,extra:any={})=>({method:'natural',k,ways,cuts:[[[1,0],[2,0]],[[3,0],[4,0]]],boundary:[[1,0],[4,0]],stats:{cut_edges:2,cut_km:222.4,cut_pct:4,boundary_nodes:2,contiguous:3,conductance_mean:0.0048,km_min:100,km_median:200,km_max:300},...extra});
 const clusters={method:'regions',created_at:'2026-10-20',pbf_sha256:'abc',policy_version:6,partitions:[partition(16)]} as any;
 it('makes clusters adjacent when their ways share a vertex',()=>{
  expect(clusterAdjacency(roads,ways)).toEqual({0:[1],1:[0,2],2:[1],3:[]});
 });
 it('colours adjacent clusters differently with a deterministic greedy colouring',()=>{
  const adjacency={0:[1,2],1:[0,2],2:[0,1],3:[4],4:[3],5:[]};
  const colours=colorClusters(adjacency);
  for(const [a,ns] of Object.entries(adjacency))for(const b of ns)expect(colours[Number(a)]).not.toBe(colours[b]);
  for(const c of Object.values(colours))expect(CLUSTER_COLORS).toContain(c);
  expect(colorClusters(adjacency)).toEqual(colours);
  expect(colours[5]).toBe(CLUSTER_COLORS[0]);
 });
 it('names the two clusters of each cut and its length',()=>{
  const cuts=cutInfo(partition(16) as any,roads);
  expect(cuts.map(c=>[c.a,c.b])).toEqual([[0,1],[1,2]]);
  expect(cuts[0].km).toBeCloseTo(111.19,1);
  expect(cutTipText(cuts[0])).toBe('Cắt giữa cụm 0 và cụm 1, 111,2 km');
 });
 it('takes the second cluster from the start of a cut whose far end touches no assigned way',()=>{
  const two=[road(1,{},[[0,0],[1,0]]),road(2,{},[[1,0],[2,0]])];
  const cut={method:'natural',k:2,ways:{'1':0,'2':1},cuts:[[[1,0],[1,1]]],boundary:[],stats:{}} as any;
  expect(cutInfo(cut,two).map(c=>[c.a,c.b])).toEqual([[0,1]]);
 });
 it('adds the clusters on both sides of every cut to the adjacency',()=>{
  const apart=[road(1,{},[[0,0],[1,0]]),road(2,{},[[2,0],[3,0]])];
  const cut={method:'natural',k:2,ways:{'1':0,'2':1},cuts:[[[1,0],[2,0]]],boundary:[],stats:{}} as any;
  const all=[...apart,road(7,{},[[1,0],[2,0]])];
  expect(partitionAdjacency(cut,all)).toEqual({0:[1],1:[0]});
 });
 it('labels the partition stats in Vietnamese',()=>{
  expect(clusterStatRows(partition(16).stats,16)).toEqual([['Số cạnh bị cắt','2'],['Km bị cắt','222 km'],['Tỉ lệ km bị cắt','4 %'],['Số nút biên','2'],['Số cụm liền mạch','3 / 16'],['Conductance trung bình','0,0048'],['Km mỗi cụm (nhỏ nhất / trung vị / lớn nhất)','100 / 200 / 300']]);
 });
 it('warns when the cluster file was built from another map',()=>{
  expect(clustersOutdated(clusters,{pbf_sha256:'abc'} as any)).toBe(false);
  expect(clustersOutdated(clusters,{pbf_sha256:'def'} as any)).toBe(true);
  expect(clustersOutdated(clusters,{} as any)).toBe(false);
 });
});

describe('river and province partitions (22)',()=>{
 const roads=[road(1,{},[[0,0],[1,0]]),road(3,{},[[1,0],[2,0]]),road(2,{},[[2,0],[3,0]])];
 const stats=(o:any)=>({cut_edges:257,cut_km:4072,cut_pct:6.2,boundary_nodes:482,contiguous:31,conductance_mean:0.0086,km_min:735,km_median:1793,km_max:3887,...o});
 const natural={method:'natural',k:5,ways:{'1':0,'3':1,'2':1},cuts:[[[1,0],[2,0]]],boundary:[[1,0]],stats:stats({})};
 const province={method:'province',k:34,ways:{'1':0,'3':1,'2':1},names:{'0':'Thành phố Hà Nội','1':'Tỉnh Bắc Ninh'},cuts:[[[1,0],[2,0]]],boundary:[[1,0]],stats:stats({cut_edges:300,cut_km:3900,cut_pct:5.9,boundary_nodes:520,contiguous:34,conductance_mean:0.0086,unassigned_nodes:12})};
 const clusters={method:'regions',created_at:'2026-10-20',partitions:[province,natural]} as any;
 it('defaults to the river partition and switches to provinces when asked',()=>{
  expect(pickPartition(clusters,null)).toBe(natural);
  expect(pickPartition(clusters,'natural')).toBe(natural);
  expect(pickPartition(clusters,'province')).toBe(province);
  expect(provincePartition(clusters)).toBe(province);
  expect(naturalPartition(clusters)).toBe(natural);
 });
 it('defaults to provinces when the file has no river partition',()=>{
  const only={...clusters,partitions:[province]};
  expect(naturalPartition(only)).toBeNull();
  expect(pickPartition(only,null)).toBe(province);
  expect(pickPartition(only,'natural')).toBe(province);
  expect(pickPartition({...clusters,partitions:[natural]},'province')).toBe(natural);
  expect(pickPartition({...clusters,partitions:[]},null)).toBeNull();
 });
 it('names provinces in cut tooltips',()=>{
  const cuts=cutInfo(province as any,roads);
  expect(cutTipText(cuts[0])).toBe('Cắt giữa Thành phố Hà Nội và Tỉnh Bắc Ninh, 111,2 km');
  expect(cutTipText(cutInfo(natural as any,roads)[0])).toBe('Cắt giữa cụm 0 và cụm 1, 111,2 km');
 });
 it('labels the stats panel and adds unassigned nodes for provinces',()=>{
  expect(partitionLabel(natural as any)).toBe('Phân cụm · theo sông');
  expect(partitionLabel(province as any)).toBe('Phân cụm · theo tỉnh');
  const rows=clusterStatRows(province.stats,34);
  expect(rows).toContainEqual(['Số cụm liền mạch','34 / 34']);
  expect(rows[rows.length-1]).toEqual(['Số nút chưa gán','12']);
  expect(clusterStatRows(natural.stats,5).map(r=>r[0])).not.toContain('Số nút chưa gán');
 });
});

describe('group chips',()=>{
 it('keeps roads, bridges and ferries of the enabled groups only',()=>{
  const features=[road(1,{group:'caotoc'}),road(2,{group:'quoclo',bridge:true,bridge_kind:'ferry'}),road(3,{group:'tinhlo'}),road(4,{group:'quoclo',bridge:true,bridge_kind:'small'})];
  expect(visibleGroups(features,{caotoc:true,quoclo:false,tinhlo:true},true).map((f:any)=>f.properties.way_id)).toEqual([1,3]);
  expect(visibleGroups(features,{caotoc:false,quoclo:true,tinhlo:false},true).map((f:any)=>f.properties.way_id)).toEqual([2,4]);
  expect(visibleGroups(features,{caotoc:false,quoclo:true,tinhlo:false},false).map((f:any)=>f.properties.way_id)).toEqual([4]);
 });
});

describe('review list',()=>{
 const junctions=[point({type:'deadend',node_id:11,big_road:'QL.1',reason:'construction',mainline:true},[105.7,21]),point({type:'deadend',node_id:8,big_road:'ĐT.70',reason:'end',mainline:false},[106,21.1]),point({type:'deadend',node_id:12,big_road:'CT.40',reason:'continues',mainline:true},[106.5,20.9]),point({type:'B',node_id:2},[105,21])];
 const roads=[road(70,{bridge:true,bridge_kind:'ferry',ferry_name:'Phà Đình Khao',car_confirmed:false},[[106,10],[106.02,10]]),road(70,{bridge:true,bridge_kind:'ferry',ferry_name:'Phà Đình Khao',car_confirmed:false},[[106.02,10],[106.04,10]]),road(71,{bridge:true,bridge_kind:'ferry',ferry_name:'Phà Đại Ngãi',car_confirmed:true})];
 const meta={osrm_error_samples:[{node_id:99,at:[107,16],error:'NoSegment'}]} as any;
 const review={'12':{status:'seen',note:'đã kiểm',updated_at:'2026-10-20T00:00:00Z'},'ferry:70':{status:'skipped',note:'',updated_at:'2026-10-20T00:00:00Z'}} as any;
 const items=reviewItems({junctions,roads,meta},review);
 it('lists dead ends, unconfirmed ferries once per way, and OSRM errors with their review state',()=>{
  expect(items.map(i=>[i.kind,i.key,i.status])).toEqual([['deadend','11','unseen'],['deadend','8','unseen'],['deadend','12','seen'],['ferry','ferry:70','skipped'],['osrm','99','unseen']]);
  expect(items.find(i=>i.key==='12')).toMatchObject({route:'CT.40',reason:'continues',note:'đã kiểm',at:[106.5,20.9],mainline:true});
  expect(items.find(i=>i.key==='ferry:70')).toMatchObject({route:'Phà Đình Khao',osmType:'way',osmId:70,at:[106.02,10]});
  expect(items.find(i=>i.kind==='osrm')).toMatchObject({osmType:'node',osmId:99,at:[107,16],reason:'NoSegment'});
 });
 it('shows mainline dead ends only until "all" is on, and filters by reason and status',()=>{
  const base={all:false,reason:'',status:''};
  expect(filterReview(items,base).map(i=>i.key)).toEqual(['11','12','ferry:70','99']);
  expect(filterReview(items,{...base,all:true}).map(i=>i.key)).toEqual(['11','8','12','ferry:70','99']);
  expect(filterReview(items,{...base,all:true,reason:'end'}).map(i=>i.key)).toEqual(['8']);
  expect(filterReview(items,{...base,status:'unseen'}).map(i=>i.key)).toEqual(['11','99']);
 });
 it('counts remaining unseen items against the total',()=>{
  expect(reviewCounts(items)).toEqual({left:3,total:5});
 });
 it('links each item to OSM',()=>{
  expect(osmViewUrl(items[0])).toBe('https://www.openstreetmap.org/node/11');
  expect(osmEditUrl(items[0])).toBe('https://www.openstreetmap.org/edit?node=11');
  const ferry=items.find(i=>i.kind==='ferry')!;
  expect(osmViewUrl(ferry)).toBe('https://www.openstreetmap.org/way/70');
  expect(osmEditUrl(ferry)).toBe('https://www.openstreetmap.org/edit?way=70');
 });
 it('exports CSV with a UTF-8 BOM and the agreed columns',()=>{
  const csv=reviewCsv([items[2],items[3]]);
  expect(csv.charCodeAt(0)).toBe(0xfeff);
  const lines=csv.slice(1).split('\r\n');
  expect(lines[0]).toBe('loại,khóa,tuyến,lý do,trạng thái,ghi chú,kinh độ,vĩ độ');
  expect(lines[1]).toBe('Điểm cụt,12,CT.40,Đường chạy tiếp nhưng OSRM không nối được trong 2 km,đã xem,đã kiểm,106.5,20.9');
  expect(lines[2]).toBe('Phà chưa xác nhận,ferry:70,Phà Đình Khao,OSM chưa ghi chở ô tô,bỏ qua,,106.02,10');
 });
 it('quotes CSV fields that contain commas, quotes or line breaks',()=>{
  const csv=reviewCsv([{...items[0],note:'a, "b"\nc'}]);
  expect(csv.slice(1).split('\r\n')[1]).toBe('Điểm cụt,11,QL.1,Đường đang xây,chưa xem,"a, ""b""\nc",105.7,21');
 });
});
