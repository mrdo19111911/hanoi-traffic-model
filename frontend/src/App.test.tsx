import {render,screen,waitFor,fireEvent,act,within} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {vi,describe,it,expect,beforeEach} from 'vitest';
import App from './App';
import {buildingStore} from './buildings';
const map=vi.hoisted(()=>({props:null as any}));
vi.mock('./TrafficMap',()=>({default:(props:any)=>{map.props=props;return <div data-testid="traffic-map">Bản đồ</div>;}}));
const bigRoads={type:'FeatureCollection',features:[{type:'Feature',geometry:{type:'LineString',coordinates:[[105.8,21],[105.9,21]]},properties:{way_id:1,group:'quoclo',highway:'trunk',ref:'QL.1',name:'Ngọc Hồi',oneway:false,ref_only:false,component:0,isolated:false,island:false,km:10.4,speed_kmh:68,speed_source:'profile_default'}},{type:'Feature',geometry:{type:'LineString',coordinates:[[104,10.2],[104.01,10.2]]},properties:{way_id:9,group:'quoclo',highway:'trunk',ref:'QL.80',name:'Phú Quốc',oneway:false,ref_only:false,component:3,isolated:true,island:true,km:12.5}},{type:'Feature',geometry:{type:'LineString',coordinates:[[105.9,21],[106,21.1]]},properties:{way_id:5,group:'tinhlo',highway:'secondary',ref:'ĐT.70',name:'',oneway:false,ref_only:true,bridge:false,component:0,isolated:false,island:false,fragment:false,km:8.2}},{type:'Feature',geometry:{type:'LineString',coordinates:[[105.7,21],[105.8,21]]},properties:{way_id:6,group:'quoclo',highway:'primary',ref:'',name:'Phố Huyện',oneway:false,ref_only:false,bridge:true,bridge_kind:'road',component:0,isolated:false,island:false,fragment:false,km:0.3}},{type:'Feature',geometry:{type:'LineString',coordinates:[[106.2,21.3],[106.2001,21.3]]},properties:{way_id:7,group:'quoclo',highway:'trunk_link',ref:'',name:'',oneway:false,ref_only:false,bridge:false,component:9,isolated:true,island:false,fragment:true,km:0.01}}]};
const ferry={type:'Feature',geometry:{type:'LineString',coordinates:[[106,10],[106.02,10],[106.04,10]]},properties:{way_id:70,group:'quoclo',highway:'ferry',ref:'',name:'',bridge:true,bridge_kind:'ferry',ferry_name:'Phà Đình Khao',car_confirmed:false,ferry_min:15,ferry_min_source:'osrm_5kmh',component:0,isolated:false,island:false,fragment:false,km:1.25,speed_kmh:5,speed_source:'ferry'}};
const bigJunctions={type:'FeatureCollection',features:[{type:'Feature',geometry:{type:'Point',coordinates:[105.85,21]},properties:{node_id:2,type:'B',joins:['Kim Giang'],big_road:'Ngọc Hồi (QL.1)',island:false}},{type:'Feature',geometry:{type:'Point',coordinates:[105.86,21]},properties:{node_id:3,type:'C',joins:['Quan Nhân'],big_road:'Ngọc Hồi (QL.1)',island:false}},{type:'Feature',geometry:{type:'Point',coordinates:[104.005,10.2]},properties:{node_id:4,type:'B',joins:['Bến'],big_road:'Phú Quốc (QL.80)',island:true}},{type:'Feature',geometry:{type:'Point',coordinates:[106.7116,21.9737]},properties:{node_id:null,type:'border',joins:[],big_road:'Đường Hữu Nghị (QL.1)',crossing:'Cửa khẩu Hữu Nghị',island:false}},{type:'Feature',geometry:{type:'Point',coordinates:[106,21.1]},properties:{node_id:8,type:'deadend',joins:[],big_road:'ĐT.70',reason:'end',island:false,fragment:false,mainline:false}},{type:'Feature',geometry:{type:'Point',coordinates:[105.7,21]},properties:{node_id:11,type:'deadend',joins:[],big_road:'QL.1',reason:'construction',island:false,fragment:false,mainline:true}},{type:'Feature',geometry:{type:'Point',coordinates:[106.2,21.3]},properties:{node_id:10,type:'B',joins:['(primary)'],big_road:'(trunk_link)',island:false,fragment:true}}]};
const bigLand={type:'Feature',geometry:{type:'Polygon',coordinates:[[[102,8],[110,8],[110,23.5],[102,23.5],[102,8]]]},properties:{points:5}};
const bigBoundary={type:'Feature',geometry:{type:'Polygon',coordinates:[[[101,7],[111,7],[111,24],[101,24],[101,7]]]},properties:{source:'osm_admin_level_2',tolerance_m:100,points:5}};
const bigMeta={ways:2,km:{caotoc:7354.951,quoclo:30986.02,tinhlo:28146},junctions:{B:9753,C:7773,border:32,deadend:1012},components:170,island_km:412.3,land_status:'ok',bridges:2400,bridge_km:812.4,fragments:74,fragment_km:21.3,osrm_bridging:'ok',osrm_bridges:310,osrm_errors:1,osrm_unmatched:0,bridge_km_by_kind:{road:640.2,small:120.1,ferry:52.1},ferries:12,ferries_unconfirmed:5,dead_end_reasons:{construction:40,continues:300,end:230},osrm_error_samples:[{node_id:99,at:[107,16],error:'NoSegment'}],pbf_sha256:'pbf-1'};
const stats=(cut_edges:number)=>({cut_edges,cut_km:2642,cut_pct:4,boundary_nodes:313,contiguous:15,conductance_mean:0.0048,km_min:2388,km_median:3189,km_max:7185});
const cut=[[[105.9,21],[105.9,21.05]]];
const naturalPart={method:'natural',k:2,ways:{'1':0,'5':1,'6':1},rivers:['Sông Hồng'],extra_gates:[],cuts:cut,boundary:[[105.9,21]],stats:{...stats(12),contiguous:2,unassigned_nodes:0}};
const provincePart={method:'province',k:34,ways:{'1':0,'5':1,'6':0},names:{'0':'Thành phố Hà Nội','1':'Tỉnh Bắc Ninh'},cuts:cut,boundary:[[105.9,21]],stats:{...stats(300),cut_km:2500,cut_pct:3.8,boundary_nodes:520,contiguous:34,unassigned_nodes:12}};
// Spec 22: two partitions; the province one comes first here so the default cannot depend on file order.
const clusterFile={method:'regions',created_at:'2026-10-20T09:00:00+07:00',pbf_sha256:'pbf-1',policy_version:6,partitions:[provincePart,naturalPart]};
const naturalOnly={...clusterFile,partitions:[naturalPart]};
const provinceOnly={...clusterFile,partitions:[provincePart]};
const provinces={type:'FeatureCollection',pbf_sha256:'pbf-1',policy_version:6,failed:[],features:[{type:'Feature',geometry:{type:'MultiPolygon',coordinates:[[[[105,20.5],[106.2,20.5],[106.2,21.5],[105,21.5],[105,20.5]]]]},properties:{iso:'VN-HN',name:'Thành phố Hà Nội',relation_id:1,area_km2:3359,points:5}}]};
const trial={segments:[{on_a:false,coords:[[105.85,21.03],[105.86,21.03]],km:1.2,seconds:120},{on_a:true,coords:[[105.86,21.03],[106.6,20.85]],km:98.3,seconds:4500},{on_a:false,coords:[[106.6,20.85],[106.68,20.86]],km:2.5,seconds:300}],km:102,minutes:82,km_on_a:98.3,km_off_a:3.7};
const options={road_classes:[{id:'motorway',label:'Cao tốc',color:'#8b5cf6'},{id:'primary',label:'Đường chính',color:'#0c9f9a'}],methods:[{id:'idw1',label:'IDW · p = 1'},{id:'idw2',label:'IDW · p = 2'}],default_method:'idw1'};
const ok=(body:unknown)=>({ok:true,json:async()=>body});
const missing=()=>({ok:false,status:404,json:async()=>({detail:'Chưa dựng bản đồ đường lớn.'})});
let fetchMock:ReturnType<typeof vi.fn>;
beforeEach(()=>{try{localStorage.clear();}catch{/* storage blocked */}fetchMock=vi.fn(async(url:string,init?:RequestInit)=>{
 if(url==='/api/options')return ok(options);
 if(url==='/api/health')return ok({status:'ok',osrm_ready:true,api_key_configured:true,samples_ready:true,big_roads_current:true});
 if(url==='/api/big-roads')return ok(bigRoads);
 if(url==='/api/big-roads/junctions')return ok(bigJunctions);
 if(url==='/api/big-roads/meta')return ok(bigMeta);
 if(url==='/api/big-roads/land')return ok(bigLand);
 if(url==='/api/big-roads/boundary')return missing();
 if(url==='/api/big-roads/clusters')return missing();
 if(url==='/api/big-roads/provinces')return missing();
 if(url==='/api/big-roads/review')return ok({items:{}});
 if(url.startsWith('/api/big-roads/review/')&&init?.method==='PUT')return ok({...JSON.parse(init.body as string),updated_at:'2026-10-20T08:00:00+07:00'});
 if(url==='/api/big-roads/route')return ok(trial);
 if(url==='/api/data/status')return ok({sample_count:4286,by_class:{motorway:24,primary:4262},by_slot:{'0800':20}});
 if(url==='/api/experiments')return ok({status:'not_run',scores:{},limitations:[]});
 if(url==='/api/estimate')return ok({minutes:42.3,km:28.4,geometry:{type:'LineString',coordinates:[]},segments:[],by_class:{},warnings:['Một số đoạn có ít mẫu.'],method:JSON.parse(init!.body as string).method});
 return {ok:false,status:503,json:async()=>({detail:'OSRM chưa sẵn sàng'})};
 });vi.stubGlobal('fetch',fetchMock);});
const override=(routes:Record<string,(init?:RequestInit)=>unknown>)=>{const original=fetchMock.getMockImplementation()!;fetchMock.mockImplementation(async(url:string,init?:RequestInit)=>routes[url]?routes[url](init):original(url,init));};
describe('route workspace',()=>{
 it('submits coordinates and selected method then shows actual result and warnings',async()=>{
  render(<App/>);await screen.findByText('4.286');
  await userEvent.selectOptions(screen.getByLabelText('Phương pháp nội suy'),'idw2');
  await userEvent.click(screen.getByRole('button',{name:'Ước lượng hành trình'}));
  await screen.findByText('42,3');expect(screen.getByText('28,4')).toBeInTheDocument();expect(screen.getByText('Một số đoạn có ít mẫu.')).toBeInTheDocument();
  const call=fetchMock.mock.calls.find(([url])=>url==='/api/estimate')!;
  expect(JSON.parse(call[1].body)).toMatchObject({origin:{lat:21.0285,lon:105.8525},destination:{lat:21.2187,lon:105.8042},method:'idw2'});
 });
 it('swaps both endpoint coordinates',async()=>{render(<App/>);await screen.findByText('4.286');await userEvent.click(screen.getByRole('button',{name:'Đổi điểm đi và điểm đến'}));expect(screen.getByLabelText('Vĩ độ điểm đi')).toHaveValue('21.2187');expect(screen.getByLabelText('Kinh độ điểm đến')).toHaveValue('105.8525');});
 it('keeps API failure visible without showing a successful journey',async()=>{override({'/api/estimate':()=>({ok:false,status:503,json:async()=>({detail:'OSRM chưa sẵn sàng'})})});render(<App/>);await screen.findByText('4.286');await userEvent.click(screen.getByRole('button',{name:'Ước lượng hành trình'}));expect(await screen.findByRole('alert')).toHaveTextContent('OSRM chưa sẵn sàng');expect(screen.queryByText('42,3')).not.toBeInTheDocument();});
 it('rejects invalid coordinates before calling estimate',async()=>{render(<App/>);await screen.findByText('4.286');fireEvent.change(screen.getByLabelText('Vĩ độ điểm đi'),{target:{value:'100'}});await userEvent.click(screen.getByRole('button',{name:'Ước lượng hành trình'}));expect(await screen.findByRole('alert')).toHaveTextContent('Tọa độ');expect(fetchMock.mock.calls.some(([url])=>url==='/api/estimate')).toBe(false);});
 it('polls a crawl job and refreshes coverage only on completion',async()=>{override({'/api/crawl':()=>ok({id:'job-1',status:'queued'}),'/api/jobs/job-1':()=>ok({id:'job-1',status:'completed',result:{new_samples:4}})});render(<App/>);await screen.findByText('4.286');await userEvent.click(screen.getByRole('tab',{name:'Dữ liệu & kiểm định'}));await userEvent.click(screen.getByRole('button',{name:'Thu thập dữ liệu'}));await screen.findByText('Hoàn tất');expect(fetchMock.mock.calls.some(([url])=>url==='/api/jobs/job-1')).toBe(true);await waitFor(()=>expect(fetchMock.mock.calls.filter(([url])=>url==='/api/data/status').length).toBeGreaterThan(1));});
 it('shows the basemap caption only while the basemap is on',async()=>{
  render(<App/>);await screen.findByText('4.286');
  expect(screen.getByText('Bản đồ nền cần kết nối mạng')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button',{name:'Ảnh nền'}));
  expect(screen.queryByText('Bản đồ nền cần kết nối mạng')).not.toBeInTheDocument();
 });
});
const toggle=()=>screen.getByRole('button',{name:'Chỉ đường lớn'});
const types=()=>map.props.bigRoads.junctions.features.map((f:any)=>f.properties.type);
const wayIds=()=>map.props.bigRoads.roads.features.map((f:any)=>f.properties.way_id);
async function enterBig(){render(<App/>);await screen.findByText('4.286');await userEvent.click(toggle());await waitFor(()=>expect(map.props.bigRoads).toBeTruthy());}
// Spec 20: big-road panels live in drawer tabs; the layer menu is in the "Lớp" tab.
const openTab=(name:string)=>userEvent.click(screen.getByRole('tab',{name}));
async function openLayers(){const tab=screen.getByRole('tab',{name:'Lớp'});if(tab.getAttribute('aria-selected')!=='true')await userEvent.click(tab);if(!screen.queryByRole('checkbox',{name:'Giao với B'}))await userEvent.click(screen.getByRole('button',{name:'Lớp'}));}
const zoomTo=(z:number)=>act(()=>map.props.onZoomChange(z));
describe('big-road mode',()=>{
 it('loads the big-road network once and hands it to the map',async()=>{
  render(<App/>);await screen.findByText('4.286');expect(map.props.bigRoads).toBeFalsy();
  await userEvent.click(toggle());
  await waitFor(()=>expect(map.props.bigRoads?.roads.features).toHaveLength(3));
  expect(toggle()).toHaveAttribute('aria-pressed','true');
  zoomTo(12);
  expect(map.props.bigRoads.junctions.features).toHaveLength(5);
  expect(map.props.bigRoads.land.geometry.type).toBe('Polygon');
  await userEvent.click(toggle());expect(map.props.bigRoads).toBeFalsy();
  await userEvent.click(toggle());await waitFor(()=>expect(map.props.bigRoads?.roads.features).toHaveLength(3));
  expect(fetchMock.mock.calls.filter(([url])=>url==='/api/big-roads').length).toBe(1);
 });
 it('starts with an unknown zoom and draws no B/C or minor dead ends until the map reports it',async()=>{
  await enterBig();
  expect(types()).toEqual(['border','deadend']);
  expect(map.props.bigRoads.compact).toBe(true);
  expect(screen.getByText('Phóng to để xem điểm giao B/C')).toBeInTheDocument();
  zoomTo(12);
  expect(types()).toEqual(['B','C','border','deadend','deadend']);
  expect(map.props.bigRoads.compact).toBe(false);
 });
 it('shows B and C junctions and minor dead ends only from zoom 11',async()=>{
  await enterBig();
  zoomTo(10);expect(types()).toEqual(['border','deadend']);
  expect(screen.getByText('Phóng to để xem điểm giao B/C')).toBeInTheDocument();
  zoomTo(11);expect(types()).toEqual(['B','C','border','deadend','deadend']);
  expect(screen.queryByText('Phóng to để xem điểm giao B/C')).not.toBeInTheDocument();
 });
 it('draws small border squares and dead-end rings below zoom 8',async()=>{
  await enterBig();
  zoomTo(7);expect(map.props.bigRoads.compact).toBe(true);
  zoomTo(8);expect(map.props.bigRoads.compact).toBe(false);
 });
 it('cuts the mask along the national boundary when the API has it',async()=>{
  override({'/api/big-roads/boundary':()=>ok(bigBoundary)});
  await enterBig();
  expect(map.props.bigRoads.boundary).toEqual(bigBoundary);
  expect(map.props.bigRoads.land).toEqual(bigLand);
 });
 it('falls back to the land ring when the boundary is missing',async()=>{
  await enterBig();
  expect(map.props.bigRoads.boundary).toBeNull();
  expect(map.props.bigRoads.land).toEqual(bigLand);
 });
 it('collapses the planner into a rail and reopens it from the rail button',async()=>{
  await enterBig();
  expect(screen.queryByRole('button',{name:'Ước lượng hành trình'})).not.toBeInTheDocument();
  const rail=screen.getByRole('button',{name:'Lập hành trình'});
  expect(rail.closest('.rail')).not.toBeNull();
  await userEvent.click(rail);
  expect(map.props.bigRoads).toBeFalsy();expect(toggle()).toHaveAttribute('aria-pressed','false');
  expect(screen.getByRole('button',{name:'Ước lượng hành trình'})).toBeInTheDocument();
 });
 it('puts the layer switches in the "Lớp" menu of the toolbar in the drawer',async()=>{
  await enterBig();zoomTo(12);
  const toolbar=screen.getByRole('toolbar',{name:'Công cụ bản đồ đường lớn'});
  expect(screen.getByRole('complementary',{name:'Ngăn kéo'})).toContainElement(toolbar);
  await openLayers();
  for(const name of ['Giao với B','Giao với C','Giao biên giới','Điểm cụt','Phà','Ảnh nền'])expect(within(toolbar).getByRole('checkbox',{name})).toBeInTheDocument();
  await userEvent.click(screen.getByRole('checkbox',{name:'Giao với B'}));
  expect(types()).toEqual(['C','border','deadend','deadend']);
 });
 it('hides border crossings when their switch is cleared',async()=>{
  await enterBig();zoomTo(12);await openLayers();
  const border=screen.getByRole('checkbox',{name:'Giao biên giới'});expect(border).toBeChecked();
  await userEvent.click(border);
  expect(types()).toEqual(['B','C','deadend','deadend']);
 });
 it('shows mainline dead ends at every zoom, others only from zoom 11, until their switch is cleared',async()=>{
  await enterBig();
  const deadIds=()=>map.props.bigRoads.junctions.features.filter((f:any)=>f.properties.type==='deadend').map((f:any)=>f.properties.node_id);
  zoomTo(5);expect(deadIds()).toEqual([11]);
  zoomTo(11);expect(deadIds()).toEqual([8,11]);
  await openLayers();const dead=screen.getByRole('checkbox',{name:'Điểm cụt'});expect(dead).toBeChecked();
  await userEvent.click(dead);
  expect(types()).not.toContain('deadend');
 });
 it('hides ferries when the "Phà" switch is cleared',async()=>{
  override({'/api/big-roads':()=>ok({...bigRoads,features:[...bigRoads.features,ferry]})});
  await enterBig();
  expect(wayIds()).toContain(70);
  await openLayers();await userEvent.click(screen.getByRole('checkbox',{name:'Phà'}));
  expect(wayIds()).not.toContain(70);
 });
 it('shows the API error and leaves the mode off when loading fails',async()=>{
  override({'/api/big-roads':()=>({ok:false,status:404,json:async()=>({detail:'Chưa dựng bản đồ đường lớn. Chạy python model/build_big_roads.py.'})})});
  render(<App/>);await screen.findByText('4.286');await userEvent.click(toggle());
  expect(await screen.findByRole('alert')).toHaveTextContent('Chưa dựng bản đồ đường lớn');
  expect(toggle()).toHaveAttribute('aria-pressed','false');expect(map.props.bigRoads).toBeFalsy();
 });
 it('hides island roads and junctions from the map',async()=>{
  await enterBig();zoomTo(12);
  expect(map.props.bigRoads.roads.features.some((f:any)=>f.properties.island)).toBe(false);
  expect(map.props.bigRoads.junctions.features.map((f:any)=>f.properties.node_id)).toEqual([2,3,null,8,11]);
 });
 it('keeps the basemap on in big-road mode and leaves the switch to the user',async()=>{
  render(<App/>);await screen.findByText('4.286');
  expect(map.props.basemap).toBe(true);expect(screen.getByRole('button',{name:'Ảnh nền'})).toHaveAttribute('aria-pressed','true');
  await userEvent.click(toggle());await waitFor(()=>expect(map.props.bigRoads).toBeTruthy());
  expect(map.props.basemap).toBe(true);
  await openLayers();await userEvent.click(screen.getByRole('checkbox',{name:'Ảnh nền'}));expect(map.props.basemap).toBe(false);
  await userEvent.click(toggle());expect(map.props.bigRoads).toBeFalsy();expect(map.props.basemap).toBe(false);
  await userEvent.click(screen.getByRole('button',{name:'Ảnh nền'}));expect(map.props.basemap).toBe(true);
 });
 it('keeps the basemap and explains when the land outline is missing',async()=>{
  override({'/api/big-roads/land':missing});
  render(<App/>);await screen.findByText('4.286');await userEvent.click(toggle());
  await waitFor(()=>expect(map.props.bigRoads?.roads.features).toHaveLength(3));
  expect(map.props.bigRoads.land).toBeNull();expect(map.props.basemap).toBe(true);
  expect(screen.getByText(/Chưa có hình đất liền; đang dùng ảnh nền/)).toBeInTheDocument();
 });
 it('hides short isolated fragments with their junctions and draws provincial roads underneath',async()=>{
  await enterBig();zoomTo(12);
  expect(wayIds()).toEqual([5,1,6]);
  expect(map.props.bigRoads.junctions.features.some((f:any)=>f.properties.fragment)).toBe(false);
 });
 it('summarises the network in stat cards in the drawer overview tab',async()=>{
  await enterBig();await openTab('Tổng quan');
  const stats=screen.getByRole('list',{name:'Số liệu mạng A'});
  expect(screen.getByTestId('traffic-map').closest('.map-panel')).not.toContainElement(stats);
  expect(within(stats).getByText('66.487 km')).toBeInTheDocument();
  expect(within(stats).getByText(/đã ẩn 412,3 km trên đảo/)).toBeInTheDocument();
  expect(within(stats).getByText(/đã ẩn 74 mảnh ngắn/)).toBeInTheDocument();
  expect(within(stats).getByText('1 / 1.012')).toBeInTheDocument();
  expect(within(stats).getByText('12 tuyến')).toBeInTheDocument();
  expect(within(stats).getByText('812,4 km')).toBeInTheDocument();
  expect(within(stats).getByText('170')).toBeInTheDocument();
  expect(within(stats).getByText(/32 biên giới/)).toBeInTheDocument();
 });
 it('renders the symbol legend over the map with the basemap caption only while the basemap is on',async()=>{
  await enterBig();
  const legend=document.querySelector('.big-road-legend') as HTMLElement;
  expect(legend).not.toBeNull();
  expect(screen.getByTestId('traffic-map').closest('.map-panel')).toContainElement(legend);
  for(const label of ['Đường tỉnh','Nối bù','Phà','Điểm cụt'])expect(within(legend).getByText(label)).toBeInTheDocument();
  expect(within(legend).getByText('Bản đồ nền cần kết nối mạng')).toBeInTheDocument();
  expect(screen.getAllByText('Bản đồ nền cần kết nối mạng')).toHaveLength(1);
  await openLayers();await userEvent.click(screen.getByRole('checkbox',{name:'Ảnh nền'}));
  expect(screen.queryByText('Bản đồ nền cần kết nối mạng')).not.toBeInTheDocument();
 });
 it('warns when the big-road build is older than the map',async()=>{
  override({'/api/health':()=>ok({status:'ok',osrm_ready:true,api_key_configured:true,samples_ready:true,big_roads_current:false})});
  render(<App/>);await screen.findByText('4.286');await userEvent.click(toggle());
  expect(await screen.findByText(/dựng từ bản OSM cũ hơn/)).toBeInTheDocument();
 });
 it('puts the map notes in the drawer so they cannot cover the map buttons',async()=>{
  await enterBig();zoomTo(6);
  const note=screen.getByText('Phóng to để xem điểm giao B/C');
  expect(screen.getByRole('complementary',{name:'Ngăn kéo'})).toContainElement(note);
  expect(screen.getByTestId('traffic-map').closest('.map-panel')).not.toContainElement(note);
  expect(screen.queryByText(/Chưa nối bù qua OSRM/)).not.toBeInTheDocument();
 });
 it('notes when OSRM bridging was skipped by the builder',async()=>{
  override({'/api/big-roads/meta':()=>ok({...bigMeta,osrm_bridging:'skipped: OSRM không chạy'})});
  render(<App/>);await screen.findByText('4.286');await userEvent.click(toggle());
  expect(await screen.findByText('Chưa nối bù qua OSRM: OSRM không chạy')).toBeInTheDocument();
 });
 it('collapses and reopens the side column',async()=>{
  await enterBig();await openTab('Duyệt');
  expect(screen.getByRole('region',{name:'Việc cần rà'})).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button',{name:'Thu gọn cột phụ'}));
  expect(screen.queryByRole('region',{name:'Việc cần rà'})).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button',{name:'Mở cột phụ'}));
  expect(screen.getByRole('region',{name:'Việc cần rà'})).toBeInTheDocument();
 });
});
describe('group chips',()=>{
 it('hide and show each road group and note when every group is off',async()=>{
  await enterBig();
  const chip=(name:string)=>within(screen.getByRole('toolbar',{name:'Công cụ bản đồ đường lớn'})).getByRole('button',{name});
  for(const name of ['Cao tốc','Quốc lộ','Tỉnh lộ'])expect(chip(name)).toHaveAttribute('aria-pressed','true');
  await userEvent.click(chip('Quốc lộ'));
  expect(chip('Quốc lộ')).toHaveAttribute('aria-pressed','false');
  expect(wayIds()).toEqual([5]);
  await userEvent.click(chip('Tỉnh lộ'));await userEvent.click(chip('Cao tốc'));
  expect(wayIds()).toEqual([]);
  expect(screen.getByText('Đã tắt mọi nhóm đường')).toBeInTheDocument();
  await userEvent.click(chip('Quốc lộ'));
  expect(wayIds()).toEqual([1,6]);
  expect(screen.queryByText('Đã tắt mọi nhóm đường')).not.toBeInTheDocument();
 });
});
describe('review panel',()=>{
 const panel=()=>screen.getByRole('region',{name:'Việc cần rà'});
 const rows=()=>within(panel()).queryAllByRole('listitem');
 it('lists mainline dead ends and OSRM errors with counts, and all dead ends on request',async()=>{
  override({'/api/big-roads/review':()=>ok({items:{'11':{status:'seen',note:'đã kiểm',updated_at:'2026-10-20T00:00:00Z'}}})});
  await enterBig();await openTab('Duyệt');
  await waitFor(()=>expect(within(panel()).getByText('còn 1 / tổng 2')).toBeInTheDocument());
  expect(rows()).toHaveLength(2);
  expect(within(rows()[0]).getByText('QL.1')).toBeInTheDocument();
  expect(within(rows()[0]).getByText('Đường đang xây')).toBeInTheDocument();
  expect(within(rows()[1]).getByText('OSRM báo lỗi: NoSegment')).toBeInTheDocument();
  await userEvent.click(within(panel()).getByRole('checkbox',{name:'Tất cả điểm cụt'}));
  expect(rows()).toHaveLength(3);expect(within(panel()).getByText('còn 2 / tổng 3')).toBeInTheDocument();
  await userEvent.selectOptions(within(panel()).getByLabelText('Lọc theo lý do'),'end');
  expect(rows()).toHaveLength(1);expect(within(rows()[0]).getByText('ĐT.70')).toBeInTheDocument();
  await userEvent.selectOptions(within(panel()).getByLabelText('Lọc theo lý do'),'');
  await userEvent.selectOptions(within(panel()).getByLabelText('Lọc theo trạng thái'),'seen');
  expect(rows()).toHaveLength(1);expect(within(rows()[0]).getByText('QL.1')).toBeInTheDocument();
 });
 it('lists unconfirmed ferries once per way with an OSM way link',async()=>{
  override({'/api/big-roads':()=>ok({...bigRoads,features:[...bigRoads.features,ferry,{...ferry,geometry:{type:'LineString',coordinates:[[106.04,10],[106.05,10]]}}]})});
  await enterBig();await openTab('Duyệt');
  const row=within(panel()).getByText('Phà Đình Khao').closest('li')!;
  expect(within(panel()).getAllByText('Phà Đình Khao')).toHaveLength(1);
  expect(within(row as HTMLElement).getByText('OSM chưa ghi chở ô tô')).toBeInTheDocument();
  await userEvent.click(within(row as HTMLElement).getByRole('button',{name:/Phà Đình Khao/}));
  const view=within(panel()).getByRole('link',{name:'Xem trên OSM'});
  expect(view).toHaveAttribute('href','https://www.openstreetmap.org/way/70');
 });
 it('changes a status through PUT and updates the count',async()=>{
  await enterBig();await openTab('Duyệt');
  await waitFor(()=>expect(within(panel()).getByText('còn 2 / tổng 2')).toBeInTheDocument());
  await userEvent.selectOptions(within(rows()[0]).getByRole('combobox',{name:'Trạng thái'}),'skipped');
  await waitFor(()=>expect(within(panel()).getByText('còn 1 / tổng 2')).toBeInTheDocument());
  const call=fetchMock.mock.calls.find(([url,init])=>url==='/api/big-roads/review/11'&&init?.method==='PUT')!;
  expect(JSON.parse(call[1].body)).toEqual({status:'skipped',note:''});
 });
 it('saves a note for the selected item',async()=>{
  await enterBig();await openTab('Duyệt');
  await userEvent.click(within(rows()[0]).getByRole('button',{name:/QL\.1/}));
  await userEvent.type(within(panel()).getByLabelText('Ghi chú'),'cầu đang làm');
  await userEvent.click(within(panel()).getByRole('button',{name:'Lưu ghi chú'}));
  await waitFor(()=>expect(fetchMock.mock.calls.some(([url,init])=>url==='/api/big-roads/review/11'&&init?.method==='PUT'&&JSON.parse(init.body).note==='cầu đang làm')).toBe(true));
 });
 it('keeps the old status and shows the error when saving fails',async()=>{
  override({'/api/big-roads/review/11':()=>({ok:false,status:422,json:async()=>({detail:'Trạng thái không hợp lệ'})})});
  await enterBig();await openTab('Duyệt');
  await userEvent.selectOptions(within(rows()[0]).getByRole('combobox',{name:'Trạng thái'}),'seen');
  expect(await within(panel()).findByRole('alert')).toHaveTextContent('Trạng thái không hợp lệ');
  expect(within(rows()[0]).getByRole('combobox',{name:'Trạng thái'})).toHaveValue('unseen');
 });
 it('flies to a row on click and links it to OSM',async()=>{
  await enterBig();await openTab('Duyệt');
  await userEvent.click(within(rows()[0]).getByRole('button',{name:/QL\.1/}));
  expect(map.props.bigRoads.focus).toMatchObject({kind:'point',at:[105.7,21]});
  const view=within(panel()).getByRole('link',{name:'Xem trên OSM'}),edit=within(panel()).getByRole('link',{name:'Sửa trên OSM'});
  expect(view).toHaveAttribute('href','https://www.openstreetmap.org/node/11');
  expect(edit).toHaveAttribute('href','https://www.openstreetmap.org/edit?node=11');
  for(const link of [view,edit]){expect(link).toHaveAttribute('target','_blank');expect(link).toHaveAttribute('rel','noopener noreferrer');}
  const seq=map.props.bigRoads.focus.seq;
  await userEvent.click(within(rows()[0]).getByRole('button',{name:/QL\.1/}));
  expect(map.props.bigRoads.focus.seq).toBeGreaterThan(seq);
 });
 it('selects the matching row when a dead end is clicked on the map',async()=>{
  await enterBig();zoomTo(12);
  const dead=map.props.bigRoads.junctions.features.find((f:any)=>f.properties.node_id===8);
  act(()=>map.props.onJunctionClick(dead,[21.1,106]));
  expect(within(panel()).getByRole('checkbox',{name:'Tất cả điểm cụt'})).toBeChecked();
  const selected=rows().find(r=>r.getAttribute('aria-current')==='true')!;
  expect(within(selected).getByText('ĐT.70')).toBeInTheDocument();
 });
 it('exports the filtered list as CSV',async()=>{
  const created:Blob[]=[];
  const createObjectURL=vi.fn((blob:Blob)=>{created.push(blob);return 'blob:review';});
  Object.assign(URL,{createObjectURL,revokeObjectURL:vi.fn()});
  const click=vi.spyOn(HTMLAnchorElement.prototype,'click').mockImplementation(()=>{});
  await enterBig();await openTab('Duyệt');
  await userEvent.click(within(panel()).getByRole('button',{name:'Xuất CSV'}));
  expect(createObjectURL).toHaveBeenCalledTimes(1);expect(click).toHaveBeenCalled();
  expect(created[0].type).toBe('text/csv;charset=utf-8');
  const bytes=await new Promise<ArrayBuffer>(resolve=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result as ArrayBuffer);reader.readAsArrayBuffer(created[0]);});
  const text=new TextDecoder('utf-8',{ignoreBOM:true}).decode(bytes);
  expect(text.charCodeAt(0)).toBe(0xfeff);
  expect(text.split('\r\n')).toHaveLength(3);
 });
 it('renders OSM names as text, never as HTML',async()=>{
  override({'/api/big-roads/junctions':()=>ok({...bigJunctions,features:bigJunctions.features.map(f=>f.properties.node_id===11?{...f,properties:{...f.properties,big_road:'<img src=x onerror=alert(1)>'}}:f)})});
  await enterBig();await openTab('Duyệt');
  expect(within(panel()).getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument();
  expect(panel().querySelector('img')).toBeNull();
 });
});
describe('route search',()=>{
 const search=()=>screen.getByRole('combobox',{name:'Tìm tuyến'});
 it('suggests routes by normalised ref with their length',async()=>{
  await enterBig();
  await userEvent.type(search(),'ql1');
  const suggestions=()=>within(screen.getByRole('listbox')).getAllByRole('option');
  const options=suggestions();
  expect(options).toHaveLength(1);expect(options[0]).toHaveTextContent('QL.1');expect(options[0]).toHaveTextContent('10,4 km');
  await userEvent.clear(search());await userEvent.type(search(),'dt70');
  expect(suggestions()[0]).toHaveTextContent('ĐT.70');
  await userEvent.clear(search());await userEvent.type(search(),'pho huyen');
  expect(suggestions()[0]).toHaveTextContent('Phố Huyện');
 });
 it('highlights the chosen route, fits it, summarises it, and clears with Esc or ×',async()=>{
  await enterBig();
  await userEvent.type(search(),'ql1');await userEvent.click(screen.getByRole('option',{name:/QL\.1/}));
  const chosen=[...map.props.bigRoads.highlight].map((f:any)=>f.properties.way_id);
  expect(chosen).toEqual([1]);
  expect(map.props.bigRoads.focus).toMatchObject({kind:'bounds',bbox:[105.8,21,105.9,21]});
  const summary=screen.getByRole('region',{name:'Tuyến đã chọn'});
  expect(summary).toHaveTextContent('QL.1');expect(summary).toHaveTextContent('10,4 km');expect(summary).toHaveTextContent('1 khúc rời');expect(summary).toHaveTextContent('0 điểm cụt');
  await userEvent.type(search(),'{Escape}');
  expect(map.props.bigRoads.highlight).toBeFalsy();
  expect(screen.queryByRole('region',{name:'Tuyến đã chọn'})).not.toBeInTheDocument();
  await userEvent.type(search(),'ql1');await userEvent.click(screen.getByRole('option',{name:/QL\.1/}));
  await userEvent.click(screen.getByRole('button',{name:'Bỏ chọn tuyến'}));
  expect(map.props.bigRoads.highlight).toBeFalsy();expect(search()).toHaveValue('');
 });
});
describe('segment detail',()=>{
 const road=(id:number)=>map.props.bigRoads.roads.features.find((f:any)=>f.properties.way_id===id);
 it('shows a clicked road in the side column and returns to the review list on an empty click',async()=>{
  await enterBig();
  act(()=>map.props.onRoadClick(road(1),[21,105.85]));
  const detail=screen.getByRole('region',{name:'Chi tiết đoạn'});
  expect(detail).toHaveTextContent('QL.1');expect(detail).toHaveTextContent('Ngọc Hồi');expect(detail).toHaveTextContent('Quốc lộ');
  expect(detail).toHaveTextContent('10,38 km');
  expect(detail).toHaveTextContent('68 km/h');expect(detail).toHaveTextContent('mặc định theo profile OSRM');
  expect(within(detail).getByRole('link',{name:'Xem trên OSM'})).toHaveAttribute('href','https://www.openstreetmap.org/way/1');
  expect(screen.queryByRole('region',{name:'Việc cần rà'})).not.toBeInTheDocument();
  await userEvent.click(within(detail).getByRole('button',{name:'Tô cả tuyến'}));
  expect([...map.props.bigRoads.highlight].map((f:any)=>f.properties.way_id)).toEqual([1]);
  act(()=>map.props.onMapClick([20,105]));
  expect(screen.queryByRole('region',{name:'Chi tiết đoạn'})).not.toBeInTheDocument();
  expect(screen.getByRole('region',{name:'Việc cần rà'})).toBeInTheDocument();
 });
 it('describes bridges and ferries',async()=>{
  override({'/api/big-roads':()=>ok({...bigRoads,features:[...bigRoads.features,ferry]})});
  await enterBig();
  act(()=>map.props.onRoadClick(road(6),[21,105.75]));
  expect(screen.getByRole('region',{name:'Chi tiết đoạn'})).toHaveTextContent('Nối bù qua đường lớn thiếu số hiệu');
  act(()=>map.props.onRoadClick(road(70),[10,106.02]));
  const detail=screen.getByRole('region',{name:'Chi tiết đoạn'});
  expect(detail).toHaveTextContent('Phà Đình Khao');expect(detail).toHaveTextContent('khoảng 15 phút');expect(detail).toHaveTextContent('OSM chưa ghi chở ô tô');
  expect(detail).toHaveTextContent('5 km/h');
  expect(within(detail).getByRole('button',{name:'Tô cả tuyến'})).toBeDisabled();
 });
});
describe('speed and cluster colouring',()=>{
 it('switches colouring to speed and shows the speed scale',async()=>{
  await enterBig();
  expect(map.props.bigRoads.colorBy).toBe('group');
  await userEvent.click(within(screen.getByRole('group',{name:'Màu theo'})).getByRole('button',{name:'Vận tốc'}));
  expect(map.props.bigRoads.colorBy).toBe('speed');
  const legend=document.querySelector('.big-road-legend') as HTMLElement;
  expect(within(legend).getByText('Vận tốc tự do theo profile OSRM, chưa tính kẹt xe')).toBeInTheDocument();
  for(const label of ['dưới 30','30–50','50–70','70–90','từ 90'])expect(within(legend).getByText(new RegExp(label))).toBeInTheDocument();
 });
 it('draws province boundaries only when their layer is on',async()=>{
  override({'/api/big-roads/provinces':()=>ok(provinces)});
  await enterBig();
  expect(map.props.bigRoads.provinces).toBeFalsy();
  await openLayers();
  const box=screen.getByRole('checkbox',{name:'Ranh giới tỉnh'});
  expect(box).not.toBeChecked();
  await userEvent.click(box);
  expect(map.props.bigRoads.provinces).toEqual(provinces);
  await userEvent.click(box);
  expect(map.props.bigRoads.provinces).toBeFalsy();
 });
 it('says province boundaries have no data when the API returns 404',async()=>{
  await enterBig();await openLayers();
  expect(screen.getByText('Ranh giới tỉnh: chưa có dữ liệu')).toBeInTheDocument();
  expect(screen.queryByRole('checkbox',{name:'Ranh giới tỉnh'})).toBeNull();
 });
 it('says the cluster layer has no data when the API returns 404',async()=>{
  await enterBig();await openLayers();
  expect(screen.getByText('Cụm: chưa có dữ liệu')).toBeInTheDocument();
  expect(screen.queryByRole('checkbox',{name:'Cụm'})).toBeNull();
 });
 const picker=()=>screen.queryByRole('group',{name:'Cách chia'});
 const pickerButton=(name:string)=>within(picker()!).getByRole('button',{name});
 async function clustersOn(file:unknown=clusterFile){override({'/api/big-roads/clusters':()=>ok(file)});await enterBig();await openLayers();await userEvent.click(screen.getByRole('checkbox',{name:'Cụm'}));await openTab('Cụm');}
 it('colours roads by the river partition by default when the cluster layer is on, adjacent clusters apart',async()=>{
  await clustersOn();
  expect(map.props.bigRoads.colorBy).toBe('cluster');
  expect(map.props.bigRoads.clusters).toEqual(naturalPart.ways);
  const colours=map.props.bigRoads.clusterColors;
  expect(colours[0]).toBeTruthy();expect(colours[1]).toBeTruthy();expect(colours[0]).not.toBe(colours[1]);
  expect(document.querySelector('.big-road-legend')).toHaveTextContent('Tô theo sông · 2 vùng');
  expect(document.querySelector('.big-road-legend')).toHaveTextContent('Nhát cắt');
  expect(map.props.bigRoads.cutKey).toBe('natural-2');
 });
 it('shows exactly the "Sông (N)" and "Tỉnh (34)" buttons, Sông pressed by default (22.3)',async()=>{
  expect(picker()).toBeNull();
  await clustersOn();
  expect(within(picker()!).getAllByRole('button').map(b=>b.textContent)).toEqual(['Sông (2)','Tỉnh (34)']);
  expect(pickerButton('Sông (2)')).toHaveAttribute('aria-pressed','true');
  expect(pickerButton('Tỉnh (34)')).toHaveAttribute('aria-pressed','false');
  await openLayers();await userEvent.click(screen.getByRole('checkbox',{name:'Cụm'}));
  expect(picker()).toBeNull();expect(map.props.bigRoads.colorBy).toBe('group');expect(map.props.bigRoads.cuts).toBeFalsy();
 });
 it('defaults to Tỉnh, the only button, when the file has no natural partition (22.3)',async()=>{
  await clustersOn(provinceOnly);
  expect(within(picker()!).getAllByRole('button').map(b=>b.textContent)).toEqual(['Tỉnh (34)']);
  expect(pickerButton('Tỉnh (34)')).toHaveAttribute('aria-pressed','true');
  expect(map.props.bigRoads.clusters).toEqual(provincePart.ways);
  expect(document.querySelector('.big-road-legend')).toHaveTextContent('Tô theo tỉnh · 34 tỉnh');
 });
 it('hands cuts with their two clusters and the boundary nodes to the map',async()=>{
  await clustersOn();
  expect(map.props.bigRoads.cuts).toHaveLength(1);
  expect(map.props.bigRoads.cuts[0]).toMatchObject({a:0,b:1,coords:[[105.9,21],[105.9,21.05]]});
  expect(map.props.bigRoads.boundaryNodes).toEqual([[105.9,21]]);
 });
 it('shows the selected partition stats in the drawer cluster tab',async()=>{
  await clustersOn();
  const table=screen.getByRole('region',{name:'Phân cụm'});
  expect(within(table).getByRole('heading')).toHaveTextContent('Phân cụm · theo sông');
  expect(within(table).getByText('Số cạnh bị cắt').closest('tr')).toHaveTextContent('12');
  expect(within(table).getByText('Số cụm liền mạch').closest('tr')).toHaveTextContent('2 / 2');
  expect(within(table).getByText('Số nút chưa gán').closest('tr')).toHaveTextContent('0');
  expect(table).toHaveTextContent('regions · 2026-10-20T09:00:00+07:00');
  await userEvent.click(pickerButton('Tỉnh (34)'));
  expect(within(table).getByText('Số cạnh bị cắt').closest('tr')).toHaveTextContent('300');
  expect(within(table).getByText('Số cụm liền mạch').closest('tr')).toHaveTextContent('34 / 34');
 });
 it('switches to the province partition and names provinces on cuts',async()=>{
  await clustersOn();
  await userEvent.click(pickerButton('Tỉnh (34)'));
  expect(pickerButton('Tỉnh (34)')).toHaveAttribute('aria-pressed','true');
  expect(pickerButton('Sông (2)')).toHaveAttribute('aria-pressed','false');
  expect(map.props.bigRoads.clusters).toEqual(provincePart.ways);
  expect(map.props.bigRoads.cutKey).toBe('province-34');
  expect(map.props.bigRoads.cuts[0]).toMatchObject({aName:'Thành phố Hà Nội',bName:'Tỉnh Bắc Ninh'});
  const colours=map.props.bigRoads.clusterColors;expect(colours[0]).not.toBe(colours[1]);
  const panel=screen.getByRole('region',{name:'Phân cụm'});
  expect(within(panel).getByRole('heading')).toHaveTextContent('Phân cụm · theo tỉnh');
  expect(within(panel).getByText('Số nút chưa gán').closest('tr')).toHaveTextContent('12');
  expect(document.querySelector('.big-road-legend')).toHaveTextContent('Tô theo tỉnh · 34 tỉnh');
 });
 it('switches back to the river partition after choosing provinces',async()=>{
  await clustersOn();
  await userEvent.click(pickerButton('Tỉnh (34)'));
  await userEvent.click(pickerButton('Sông (2)'));
  expect(pickerButton('Sông (2)')).toHaveAttribute('aria-pressed','true');
  expect(pickerButton('Tỉnh (34)')).toHaveAttribute('aria-pressed','false');
  expect(map.props.bigRoads.clusters).toEqual(naturalPart.ways);
  expect(map.props.bigRoads.cuts[0].aName).toBeUndefined();
  expect(within(screen.getByRole('region',{name:'Phân cụm'})).getByRole('heading')).toHaveTextContent('Phân cụm · theo sông');
  expect(document.querySelector('.big-road-legend')).toHaveTextContent('Tô theo sông · 2 vùng');
 });
 it('offers no province button when the file has no province partition',async()=>{
  await clustersOn(naturalOnly);
  expect(within(picker()!).getAllByRole('button').map(b=>b.textContent)).toEqual(['Sông (2)']);
  expect(within(picker()!).queryByRole('button',{name:/Tỉnh/})).toBeNull();
 });
 it('has no comparison table between partitions (22)',async()=>{
  await clustersOn();
  expect(screen.queryByRole('region',{name:'So sánh cách chia'})).not.toBeInTheDocument();
 });
 it('warns when the cluster file comes from another map build',async()=>{
  await clustersOn({...clusterFile,pbf_sha256:'pbf-0'});
  expect(screen.getByText('Dữ liệu cụm dựng từ bản đồ cũ')).toBeInTheDocument();
 });
 it('does not warn when the cluster file matches the map build',async()=>{
  await clustersOn();
  expect(screen.queryByText('Dữ liệu cụm dựng từ bản đồ cũ')).not.toBeInTheDocument();
 });
});
describe('trial route',()=>{
 const panel=()=>screen.getByRole('region',{name:'Thử tuyến'});
 it('routes between the form points and draws on-A and off-A segments',async()=>{
  await enterBig();await openTab('Thử tuyến');
  await userEvent.click(within(panel()).getByRole('button',{name:'Lấy từ form'}));
  await userEvent.click(within(panel()).getByRole('button',{name:'Tìm tuyến'}));
  await within(panel()).findByText('98,3 km trên mạng A, 3,7 km ngoài mạng A, khoảng 82 phút (OSRM, chưa tính kẹt xe)');
  const call=fetchMock.mock.calls.find(([url])=>url==='/api/big-roads/route')!;
  expect(call[1].method).toBe('POST');
  expect(JSON.parse(call[1].body)).toEqual({origin:[21.0285,105.8525],destination:[21.2187,105.8042]});
  expect(map.props.bigRoads.trial.segments.map((s:any)=>s.on_a)).toEqual([false,true,false]);
 });
 it('fits the map to the trial route when it arrives',async()=>{
  await enterBig();await openTab('Thử tuyến');
  await userEvent.click(within(panel()).getByRole('button',{name:'Lấy từ form'}));
  await userEvent.click(within(panel()).getByRole('button',{name:'Tìm tuyến'}));
  await waitFor(()=>expect(map.props.bigRoads.trial).toBeTruthy());
  expect(map.props.bigRoads.focus).toMatchObject({kind:'bounds',bbox:[105.85,20.85,106.68,21.03]});
 });
 it('picks both points on the map',async()=>{
  await enterBig();await openTab('Thử tuyến');
  await userEvent.click(within(panel()).getByRole('button',{name:'Chọn điểm đi'}));
  expect(map.props.picking).toBe('origin');
  act(()=>map.props.onPick({lat:21,lon:105.9}));
  await userEvent.click(within(panel()).getByRole('button',{name:'Chọn điểm đến'}));
  const road=map.props.bigRoads.roads.features[0];
  act(()=>map.props.onRoadClick(road,[20.9,106.1]));
  expect(screen.queryByRole('region',{name:'Chi tiết đoạn'})).not.toBeInTheDocument();
  expect(map.props.bigRoads.trialPoints).toEqual({origin:[21,105.9],destination:[20.9,106.1]});
  await userEvent.click(within(panel()).getByRole('button',{name:'Tìm tuyến'}));
  await waitFor(()=>expect(map.props.bigRoads.trial).toBeTruthy());
  const call=fetchMock.mock.calls.find(([url])=>url==='/api/big-roads/route')!;
  expect(JSON.parse(call[1].body)).toEqual({origin:[21,105.9],destination:[20.9,106.1]});
 });
 it('shows the API detail when no route is found',async()=>{
  override({'/api/big-roads/route':()=>({ok:false,status:422,json:async()=>({detail:'Không tìm được tuyến giữa hai điểm'})})});
  await enterBig();await openTab('Thử tuyến');
  await userEvent.click(within(panel()).getByRole('button',{name:'Lấy từ form'}));
  await userEvent.click(within(panel()).getByRole('button',{name:'Tìm tuyến'}));
  expect(await within(panel()).findByRole('alert')).toHaveTextContent('Không tìm được tuyến giữa hai điểm');
  expect(map.props.bigRoads.trial).toBeFalsy();
 });
});

describe('map-first layout (spec 20)',()=>{
 const drawer=()=>screen.queryByRole('complementary',{name:'Ngăn kéo'});
 const mapPanel=()=>screen.getByTestId('traffic-map').closest('.map-panel') as HTMLElement;
 const drawerTabs=()=>within(screen.getByRole('tablist',{name:'Thẻ ngăn kéo'})).getAllByRole('tab');
 it('drops the page heading and footer on the journey tab and keeps them on the data tab',async()=>{
  const {container}=render(<App/>);await screen.findByText('4.286');
  expect(screen.queryByRole('heading',{level:1})).not.toBeInTheDocument();expect(container.querySelector('footer')).toBeNull();
  const tabs=screen.getByRole('tablist',{name:'Không gian làm việc'});expect(tabs.closest('.topbar')).not.toBeNull();
  await userEvent.click(screen.getByRole('tab',{name:'Dữ liệu & kiểm định'}));
  expect(screen.getByRole('heading',{level:1})).toHaveTextContent('Hiểu đường.');expect(container.querySelector('footer')).not.toBeNull();
 });
 it('puts the planner and results in the "Hành trình" drawer tab beside the map',async()=>{
  render(<App/>);await screen.findByText('4.286');
  expect(drawerTabs().map(t=>t.textContent)).toEqual(['Hành trình']);expect(drawerTabs()[0]).toHaveAttribute('aria-selected','true');
  expect(drawer()).toContainElement(screen.getByRole('button',{name:'Ước lượng hành trình'}));
  expect(drawer()).toContainElement(screen.getByText('Hành trình của bạn sẽ hiển thị ở đây'));
  expect(mapPanel()).not.toContainElement(drawer());
 });
 it('collapses the drawer from the map toggle and keeps the map shown',async()=>{
  render(<App/>);await screen.findByText('4.286');
  const button=screen.getByRole('button',{name:'Thu gọn cột phụ'});
  expect(button.closest('.map-top-right')).toContainElement(toggle());
  expect(button).toHaveAttribute('aria-expanded','true');expect(button).toHaveAttribute('aria-controls',drawer()!.id);
  const layout=map.props.layoutKey;
  await userEvent.click(button);
  expect(drawer()).not.toBeInTheDocument();
  expect(screen.getByRole('button',{name:'Mở cột phụ'})).toHaveAttribute('aria-expanded','false');
  expect(screen.getByTestId('traffic-map')).toBeInTheDocument();expect(map.props.layoutKey).not.toBe(layout);
  await userEvent.click(screen.getByRole('button',{name:'Mở cột phụ'}));
  expect(drawer()).toBeInTheDocument();
 });
 it('toggles the drawer with "[" except while typing in a field',async()=>{
  render(<App/>);await screen.findByText('4.286');
  fireEvent.keyDown(window,{key:'['});expect(drawer()).not.toBeInTheDocument();
  fireEvent.keyDown(window,{key:'['});expect(drawer()).toBeInTheDocument();
  const input=screen.getByLabelText('Vĩ độ điểm đi');input.focus();
  fireEvent.keyDown(input,{key:'['});expect(drawer()).toBeInTheDocument();
  fireEvent.keyDown(screen.getByLabelText('Ngày trong tuần'),{key:'['});expect(drawer()).toBeInTheDocument();
 });
 it('offers five drawer tabs in big-road mode with the toolbar and stats in the drawer, not on the map',async()=>{
  await enterBig();
  expect(drawerTabs().map(t=>t.textContent)).toEqual(['Lớp','Cụm','Duyệt','Thử tuyến','Tổng quan']);
  expect(screen.getByRole('tab',{name:'Lớp'})).toHaveAttribute('aria-selected','true');
  const toolbar=screen.getByRole('toolbar',{name:'Công cụ bản đồ đường lớn'});
  expect(drawer()).toContainElement(toolbar);expect(mapPanel()).not.toContainElement(toolbar);
  await userEvent.click(screen.getByRole('tab',{name:'Tổng quan'}));
  const stats=screen.getByRole('list',{name:'Số liệu mạng A'});
  expect(drawer()).toContainElement(stats);expect(mapPanel()).not.toContainElement(stats);
 });
 it('opens the tab with the segment detail when a road is selected on the map',async()=>{
  await enterBig();
  act(()=>map.props.onRoadClick(map.props.bigRoads.roads.features[1],[21,105.85]));
  expect(screen.getByRole('tab',{name:'Duyệt'})).toHaveAttribute('aria-selected','true');
  expect(drawer()).toContainElement(screen.getByRole('region',{name:'Chi tiết đoạn'}));
 });
 it('collapses the legend to a chip and reopens it',async()=>{
  render(<App/>);await screen.findByText('4.286');
  expect(mapPanel()).toContainElement(screen.getByText('LOẠI ĐƯỜNG'));
  await userEvent.click(screen.getByRole('button',{name:'Thu gọn chú giải'}));
  expect(screen.queryByText('LOẠI ĐƯỜNG')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button',{name:'Chú giải'}));
  expect(screen.getByText('LOẠI ĐƯỜNG')).toBeInTheDocument();
  await userEvent.click(toggle());await waitFor(()=>expect(map.props.bigRoads).toBeTruthy());
  expect(mapPanel()).toContainElement(screen.getByText('ĐƯỜNG LỚN'));
 });
 it('shows a compact trip summary on the map once a trip exists',async()=>{
  render(<App/>);await screen.findByText('4.286');
  expect(screen.queryByLabelText('Tóm tắt hành trình')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button',{name:'Ước lượng hành trình'}));await screen.findByText('42,3');
  const card=screen.getByLabelText('Tóm tắt hành trình');
  expect(mapPanel()).toContainElement(card);expect(card).toHaveTextContent('42,3 phút');expect(card).toHaveTextContent('28,4 km');
 });
 it('remembers the drawer state and tab across renders',async()=>{
  const first=render(<App/>);await screen.findByText('4.286');
  await userEvent.click(toggle());await waitFor(()=>expect(map.props.bigRoads).toBeTruthy());
  await userEvent.click(screen.getByRole('tab',{name:'Duyệt'}));
  await userEvent.click(screen.getByRole('button',{name:'Thu gọn cột phụ'}));
  first.unmount();
  render(<App/>);await screen.findByText('4.286');
  expect(drawer()).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button',{name:'Mở cột phụ'}));
  await userEvent.click(toggle());await waitFor(()=>expect(map.props.bigRoads).toBeTruthy());
  expect(screen.getByRole('tab',{name:'Duyệt'})).toHaveAttribute('aria-selected','true');
 });
 it('renders normally when localStorage throws',async()=>{
  vi.spyOn(Storage.prototype,'getItem').mockImplementation(()=>{throw new Error('blocked');});
  vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('blocked');});
  render(<App/>);await screen.findByText('4.286');
  expect(drawer()).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button',{name:'Thu gọn cột phụ'}));
  expect(drawer()).not.toBeInTheDocument();
 });
});
describe('building layer controls',()=>{
 it('turns the layer on by default and off with the Nhà toggle, with a floors legend',async()=>{
  render(<App/>);await screen.findByText('4.286');
  const toggle=screen.getByRole('button',{name:'Nhà'});
  expect(toggle).toHaveAttribute('aria-pressed','true');expect(map.props.buildings).toBe(true);
  expect(document.querySelector('.building-legend')).toHaveTextContent('NHÀ · SỐ TẦNG');
  for(const label of ['1–2','≥ 15','Chưa rõ'])expect(within(document.querySelector('.building-legend') as HTMLElement).getByText(label)).toBeInTheDocument();
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-pressed','false');expect(map.props.buildings).toBe(false);
  expect(document.querySelector('.building-legend')).toBeNull();
 });
 it('shows the zoom hint, download progress, the count in view and errors',async()=>{
  render(<App/>);await screen.findByText('4.286');
  act(()=>map.props.onBuildingStatus({kind:'zoom'}));
  expect(screen.getByText('Phóng to tới mức 16 để xem nhà')).toBeInTheDocument();
  act(()=>map.props.onBuildingStatus({kind:'loading',file:'ha_noi_13.parquet',bytes:760347,network:true,shown:0}));
  expect(screen.getByText('Đang tải nhà: ha_noi_13.parquet (0,8 MB)…')).toBeInTheDocument();
  act(()=>map.props.onBuildingStatus({kind:'loading',file:'ha_noi_13.parquet',bytes:760347,network:false,shown:0}));
  expect(screen.getByText('Đang đọc nhà đã lưu: ha_noi_13.parquet…')).toBeInTheDocument();
  act(()=>map.props.onBuildingStatus({kind:'ready',shown:12345,total:12345}));
  expect(screen.getByText('12.345 nhà')).toBeInTheDocument();
  act(()=>map.props.onBuildingStatus({kind:'ready',shown:15000,total:23456,offline:true}));
  expect(screen.getByText('15.000 / 23.456 nhà (gần tâm nhất) · ngoại tuyến, dùng bản đã lưu')).toBeInTheDocument();
  act(()=>map.props.onBuildingStatus({kind:'error',message:'Không tải được nhà: ha_noi_13.parquet. Kiểm tra kết nối mạng.',shown:0}));
  expect(screen.getByText('Không tải được nhà: ha_noi_13.parquet. Kiểm tra kết nối mạng.').closest('.building-status')).toHaveClass('warning');
 });
 it('shows the stored size and clears the stored files',async()=>{
  const stored=vi.spyOn(buildingStore,'storedBytes').mockResolvedValue(23.4e6);
  const clear=vi.spyOn(buildingStore,'clear').mockResolvedValue();
  render(<App/>);await screen.findByText('4.286');
  act(()=>map.props.onBuildingStatus({kind:'ready',shown:10,total:10}));
  expect(await screen.findByText('Đã lưu 23,4 MB')).toBeInTheDocument();
  stored.mockResolvedValue(0);
  await userEvent.click(screen.getByRole('button',{name:'Xóa bộ nhớ nhà'}));
  expect(clear).toHaveBeenCalledTimes(1);
  await waitFor(()=>expect(screen.queryByText(/Đã lưu/)).not.toBeInTheDocument());
 });
});
