import {useEffect,useMemo,useRef,useState} from 'react';
import {ArrowDownUp,ArrowRight,ArrowUpRight,Check,ChevronRight,Clock3,Database,FlaskConical,Layers3,LoaderCircle,LocateFixed,MapPin,Navigation,PanelLeftClose,PanelLeftOpen,Route as RouteIcon,TriangleAlert,Waypoints,X} from 'lucide-react';
import TrafficMap from './TrafficMap';
import {api,type Point,type Health,type Options,type DataStatus,type Route,type Experiment,type Job,type BigRoads,type BigRoadMeta,type BigRoadView,type BigRoadReview,type BigRoadClusters,type ReviewEntry,type ReviewStatus,type MapFocus,type TrialRoute} from './api';
import type {Feature,FeatureCollection} from 'geojson';
import {GROUPS,type Group,searchRoutes,routeStats,normalizeRef,visibleGroups,reviewItems,filterReview,availableKs,pickPartition,provincePartition,naturalPartition,twoLevelPartition,isSpectral,type PartitionChoice,partitionAdjacency,colorClusters,cutInfo,clustersOutdated,type ReviewItem,type ReviewFilter,type RouteHit} from './bigRoads';
import {BigToolbar,BigStats,BigLegend,ClusterPanel,ComparePanel,ReviewPanel,RouteSummary,SegmentDetail,TrialPanel,PartitionPicker,number,type LayerState} from './BigRoadPanels';

type LatLon=[number,number];
const days=['Thứ Hai','Thứ Ba','Thứ Tư','Thứ Năm','Thứ Sáu','Thứ Bảy','Chủ nhật'];
const initialOrigin={lat:21.0285,lon:105.8525},initialDestination={lat:21.2187,lon:105.8042};
const classLabels:Record<string,string>={motorway:'Cao tốc',trunk:'Trục lớn',primary:'Đường chính',secondary:'Đường nhánh',local:'Đường khu vực',tertiary:'Đường cấp ba',residential:'Đường dân cư'};
const statusLabels:Record<string,string>={queued:'Đang chờ',running:'Đang xử lý',completed:'Hoàn tất',failed:'Thất bại'};
// Spec 18.4: B/C junctions and minor dead ends from zoom 11; smaller border squares and rings below zoom 8.
const MIN_LOCAL_JUNCTION_ZOOM=11,MIN_FULL_MARKER_ZOOM=8;
const drawOrder=(f:Feature)=>({tinhlo:0,quoclo:1,caotoc:2} as Record<string,number>)[f.properties?.group]??1;
function stringError(e:unknown){return e instanceof Error?e.message:'Không thể kết nối máy chủ.';}
// Spec 18.15: the map fits a trial route like a searched route.
function trialBounds(trial:TrialRoute):[number,number,number,number]|null{let w=180,s=90,e=-180,n=-90;for(const seg of trial.segments)for(const [lon,lat] of seg.coords){w=Math.min(w,lon);e=Math.max(e,lon);s=Math.min(s,lat);n=Math.max(n,lat);}return w<=e?[w,s,e,n]:null;}
function pointFrom(values:string[]):Point|null{const [lat,lon]=values.map(Number);if(values.some(s=>s.trim()==='')||!Number.isFinite(lat)||!Number.isFinite(lon)||Math.abs(lat)>90||Math.abs(lon)>180)return null;return{lat,lon};}
const optional=<T,>(promise:Promise<T>)=>promise.catch(()=>null);
const allGroups:Record<Group,boolean>={caotoc:true,quoclo:true,tinhlo:true};
const defaultLayers:Omit<LayerState,'basemap'>={showB:true,showC:true,showBorder:true,showDead:true,showFerries:true,showClusters:false,showProvinces:false};
// Spec 20: the drawer beside the map. Only its open state and big-road tab are remembered; storage may be unavailable.
type BigTab='layers'|'clusters'|'review'|'trial'|'stats';
const BIG_TABS:[BigTab,string][]=[['layers','Lớp'],['clusters','Cụm'],['review','Duyệt'],['trial','Thử tuyến'],['stats','Tổng quan']];
const DRAWER_KEY='hn-traffic-lab.drawer';
interface DrawerState{open:boolean;tab:BigTab}
function readDrawer():DrawerState{try{const v=JSON.parse(window.localStorage.getItem(DRAWER_KEY)||'null');if(v&&typeof v==='object')return{open:v.open!==false,tab:BIG_TABS.some(([id])=>id===v.tab)?v.tab:'layers'};}catch{/* storage unavailable */}return{open:true,tab:'layers'};}
function writeDrawer(state:DrawerState){try{window.localStorage.setItem(DRAWER_KEY,JSON.stringify(state));}catch{/* storage unavailable */}}
const typing=(target:EventTarget|null)=>{const el=target as HTMLElement|null;return !!el&&(/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName||'')||el.isContentEditable||!!el.closest?.('[contenteditable]:not([contenteditable="false"])'));};

export default function App(){
 const [health,setHealth]=useState<Health|null>(null),[options,setOptions]=useState<Options|null>(null),[data,setData]=useState<DataStatus|null>(null),[experiments,setExperiments]=useState<Experiment|null>(null);
 const [origin,setOrigin]=useState(['21.0285','105.8525']),[destination,setDestination]=useState(['21.2187','105.8042']),[day,setDay]=useState(1),[hour,setHour]=useState('08:00'),[method,setMethod]=useState('idw1');
 const [picking,setPicking]=useState<'origin'|'destination'|null>(null),[route,setRoute]=useState<Route|null>(null),[loading,setLoading]=useState(false),[error,setError]=useState(''),[pane,setPane]=useState<'route'|'data'>('route');
 const [crawlDate,setCrawlDate]=useState(new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Bangkok'})),[slots,setSlots]=useState('0800,1730'),[maxCalls,setMaxCalls]=useState(10),[job,setJob]=useState<Job|null>(null),[jobBusy,setJobBusy]=useState(false),[jobError,setJobError]=useState('');
 // Big-road mode.
 const [bigMode,setBigMode]=useState(false),[bigData,setBigData]=useState<BigRoads|null>(null),[bigLoading,setBigLoading]=useState(false),[bigError,setBigError]=useState('');
 const [layers,setLayers]=useState(defaultLayers),[basemap,setBasemap]=useState(true),[groups,setGroups]=useState(allGroups),[colorBy,setColorBy]=useState<'group'|'speed'>('group');
 // Spec 18.2 (L1): the zoom is unknown until the map reports it, so nothing dense is drawn at country level by accident.
 const [zoom,setZoom]=useState<number|null>(null),[drawer,setDrawer]=useState<DrawerState>(readDrawer),[legendOpen,setLegendOpen]=useState(true);
 const [review,setReview]=useState<Record<string,ReviewEntry>>({}),[reviewLoadError,setReviewLoadError]=useState(''),[reviewError,setReviewError]=useState('');
 const [reviewFilter,setReviewFilter]=useState<ReviewFilter>({all:false,reason:'',status:''}),[selectedReview,setSelectedReview]=useState<string|null>(null);
 const [focus,setFocus]=useState<MapFocus|null>(null),focusSeq=useRef(0),[partChoice,setPartChoice]=useState<PartitionChoice>(null);
 const [query,setQuery]=useState(''),[routeHit,setRouteHit]=useState<RouteHit|null>(null),[detail,setDetail]=useState<Feature|null>(null);
 const [trialPoints,setTrialPoints]=useState<{origin:LatLon|null;destination:LatLon|null}>({origin:null,destination:null}),[trialPicking,setTrialPicking]=useState<'origin'|'destination'|null>(null),[trial,setTrial]=useState<TrialRoute|null>(null),[trialBusy,setTrialBusy]=useState(false),[trialError,setTrialError]=useState('');
 const mounted=useRef(true),estimateAbort=useRef<AbortController|null>(null);
 const refreshData=async(signal?:AbortSignal)=>{const results=await Promise.allSettled([api<DataStatus>('/api/data/status',{signal}),api<Health>('/api/health',{signal}),api<Experiment>('/api/experiments',{signal})]);if(!mounted.current||signal?.aborted)return;for(let i=0;i<results.length;i++){const result=results[i];if(result.status==='fulfilled'){if(i===0)setData(result.value as DataStatus);if(i===1)setHealth(result.value as Health);if(i===2)setExperiments(result.value as Experiment);}}};
 useEffect(()=>{mounted.current=true;const controller=new AbortController();api<Options>('/api/options',{signal:controller.signal}).then(value=>{setOptions(value);setMethod(value.default_method);}).catch(e=>{if(!controller.signal.aborted)setError(stringError(e));});void refreshData(controller.signal);return()=>{mounted.current=false;controller.abort();estimateAbort.current?.abort();};},[]);
 useEffect(()=>{if(!job||!['queued','running'].includes(job.status))return;let stopped=false;const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;const poll=async()=>{try{const next=await api<Job>(`/api/jobs/${encodeURIComponent(job.id)}`,{signal:controller.signal});if(stopped)return;setJob(next);if(next.status==='completed')void refreshData();if(['queued','running'].includes(next.status))timer=setTimeout(poll,1500);}catch(e){if(!stopped){setJobError(`Theo dõi tác vụ bị gián đoạn: ${stringError(e)}`);}}};void poll();return()=>{stopped=true;controller.abort();clearTimeout(timer);};},[job?.id]);
 async function estimate(event:React.FormEvent){event.preventDefault();setError('');const from=pointFrom(origin),to=pointFrom(destination);if(!from||!to){setError('Tọa độ không hợp lệ. Vĩ độ từ −90 đến 90, kinh độ từ −180 đến 180.');return;}if(from.lat===to.lat&&from.lon===to.lon){setError('Hãy chọn hai điểm khác nhau.');return;}setLoading(true);setRoute(null);const controller=new AbortController();estimateAbort.current=controller;try{const [hours,minutes]=hour.split(':').map(Number);const result=await api<Route>('/api/estimate',{method:'POST',body:JSON.stringify({origin:from,destination:to,day,hour:hours+minutes/60,method}),signal:controller.signal});if(mounted.current)setRoute(result);}catch(e){if(mounted.current&&!controller.signal.aborted)setError(stringError(e));}finally{if(mounted.current)setLoading(false);}}
 useEffect(()=>writeDrawer(drawer),[drawer]);
 const toggleDrawer=()=>setDrawer(d=>({...d,open:!d.open})),openDrawer=(tab?:BigTab)=>setDrawer(d=>({open:true,tab:tab??d.tab}));
 // Spec 20: "[" opens or closes the drawer, but not while the user types in a field.
 useEffect(()=>{if(pane!=='route')return;const onKey=(e:KeyboardEvent)=>{if(e.key!=='['||e.ctrlKey||e.metaKey||e.altKey||typing(e.target))return;toggleDrawer();};window.addEventListener('keydown',onKey);return()=>window.removeEventListener('keydown',onKey);},[pane]);
 async function startJob(kind:'crawl'|'experiments'){setJobError('');setJobBusy(true);try{const normalized=slots.split(',').map(s=>s.trim()).filter(Boolean);if(kind==='crawl'&&(!/^\d{4}-\d{2}-\d{2}$/.test(crawlDate)||normalized.length===0||normalized.some(s=>!/^([01]\d|2[0-3])[0-5]\d$/.test(s))||!Number.isInteger(maxCalls)||maxCalls<1||maxCalls>60))throw new Error('Chọn ngày, khung giờ HHMM hợp lệ và từ 1 đến 60 lượt gọi.');const result=await api<Job>(kind==='crawl'?'/api/crawl':'/api/experiments/run',{method:'POST',body:JSON.stringify(kind==='crawl'?{date:crawlDate,slots:normalized,max_calls:maxCalls}:{})});if(mounted.current)setJob(result);}catch(e){if(mounted.current)setJobError(stringError(e));}finally{if(mounted.current)setJobBusy(false);}}

 function enterBigMode(){setZoom(null);setBigMode(true);}
 function leaveBigMode(){setBigMode(false);setTrialPicking(null);}
 async function loadReview(){try{const result=await api<BigRoadReview>('/api/big-roads/review');if(mounted.current){setReview(result.items||{});setReviewLoadError('');}}catch(e){if(mounted.current)setReviewLoadError(`Chưa tải được trạng thái rà soát: ${stringError(e)}`);}}
 async function toggleBigRoads(){
  if(bigMode){leaveBigMode();return;}
  setBigError('');setPicking(null);
  if(bigData){enterBigMode();return;}
  setBigLoading(true);
  try{
   // Land, boundary and clusters are optional: without them the mode still opens (spec 8.5, 18.2, 18.10).
   const [roads,junctions,meta,land,boundary,clusters,provinces]=await Promise.all([api<FeatureCollection>('/api/big-roads'),api<FeatureCollection>('/api/big-roads/junctions'),api<BigRoadMeta>('/api/big-roads/meta'),optional(api<Feature>('/api/big-roads/land')),optional(api<Feature>('/api/big-roads/boundary')),optional(api<BigRoadClusters>('/api/big-roads/clusters')),optional(api<FeatureCollection>('/api/big-roads/provinces'))]);
   if(mounted.current){setBigData({roads,junctions,meta,land,boundary,clusters,provinces});enterBigMode();void loadReview();}
  }catch(e){if(mounted.current)setBigError(stringError(e));}
  finally{if(mounted.current)setBigLoading(false);}
 }
 // Islands and short isolated fragments stay in the data files but are not drawn; provincial roads go underneath.
 const bigMainland=useMemo(()=>{if(!bigData)return null;const shown=(f:Feature)=>!f.properties?.island&&!f.properties?.fragment;const roads=bigData.roads.features.filter(shown).sort((a,b)=>drawOrder(a)-drawOrder(b));return{roads,junctions:bigData.junctions.features.filter(shown)};},[bigData]);
 const localZoom=zoom!=null&&zoom>=MIN_LOCAL_JUNCTION_ZOOM,compact=zoom==null||zoom<MIN_FULL_MARKER_ZOOM;
 const {showB,showC,showBorder,showDead,showFerries,showClusters}=layers;
 const partition=useMemo(()=>showClusters?pickPartition(bigData?.clusters,partChoice):null,[showClusters,bigData,partChoice]);
 const province=partition?provincePartition(bigData?.clusters):null,spectral32=partition?bigData?.clusters?.partitions.find(p=>isSpectral(p)&&p.k===32)??null:null;
 const clusterOn=!!partition;
 // Spec 18.14: adjacent clusters (ways meeting at a vertex, or the two sides of a cut) get different colours.
 const clusterDrawing=useMemo(()=>partition&&bigMainland?{colors:colorClusters(partitionAdjacency(partition,bigMainland.roads)),cuts:cutInfo(partition,bigMainland.roads)}:null,[partition,bigMainland]);
 const allItems=useMemo(()=>bigData&&bigMainland?reviewItems({junctions:bigMainland.junctions,roads:bigMainland.roads,meta:bigData.meta},review):[],[bigData,bigMainland,review]);
 const reviewList=useMemo(()=>filterReview(allItems,{...reviewFilter,status:''}),[allItems,reviewFilter]);
 const suggestions=useMemo(()=>bigMainland?searchRoutes(bigMainland.roads,query):[],[bigMainland,query]);
 const selection=useMemo(()=>bigMainland&&routeHit?routeStats(bigMainland.roads,bigMainland.junctions,routeHit):null,[bigMainland,routeHit]);
 const highlight=useMemo(()=>selection?new Set(selection.features):null,[selection]);
 const shownRoads=useMemo(()=>bigMainland?visibleGroups(bigMainland.roads,groups,showFerries):[],[bigMainland,groups,showFerries]);
 const noGroups=GROUPS.every(g=>!groups[g]);
 const bigView=useMemo<BigRoadView|null>(()=>{
  if(!bigMode||!bigData||!bigMainland)return null;
  // Border crossings and mainline dead ends always show; B/C and other dead ends only from zoom 11.
  const junctions=bigMainland.junctions.filter(f=>{const type=f.properties?.type;return type==='border'?showBorder:type==='deadend'?showDead&&(localZoom||!!f.properties?.mainline):localZoom&&(type==='B'?showB:showC);});
  return{roads:{...bigData.roads,features:shownRoads},junctions:{...bigData.junctions,features:junctions},junctionKey:`${showB}-${showC}-${showBorder}-${showDead}-${localZoom}-${compact}`,roadKey:`${GROUPS.map(g=>groups[g]?1:0).join('')}-${showFerries}`,land:bigData.land,boundary:bigData.boundary??null,compact,colorBy:partition?'cluster':colorBy,clusters:partition?.ways??null,clusterColors:clusterDrawing?.colors??null,cuts:clusterDrawing?.cuts??null,boundaryNodes:partition?.boundary??null,cutKey:partition?`${partition.method??'spectral'}-${partition.k}`:'',provinces:layers.showProvinces?bigData.provinces??null:null,highlight,focus,trial,trialPoints};
 },[bigMode,bigData,bigMainland,shownRoads,showB,showC,showBorder,showDead,showFerries,localZoom,compact,groups,partition,clusterDrawing,layers.showProvinces,colorBy,highlight,focus,trial,trialPoints]);

 const flyTo=(at:[number,number])=>setFocus({seq:++focusSeq.current,kind:'point',at});
 function selectRoute(hit:RouteHit){setRouteHit(hit);setQuery(hit.label);if(bigMainland){const stats=routeStats(bigMainland.roads,bigMainland.junctions,hit);if(stats.bbox)setFocus({seq:++focusSeq.current,kind:'bounds',bbox:stats.bbox});}}
 function clearRoute(){setRouteHit(null);setQuery('');}
 useEffect(()=>{if(!routeHit)return;const onKey=(e:KeyboardEvent)=>{if(e.key==='Escape')clearRoute();};window.addEventListener('keydown',onKey);return()=>window.removeEventListener('keydown',onKey);},[routeHit]);
 function paintRoute(ref:string){const key=normalizeRef(ref);selectRoute({id:`ref:${key}`,kind:'ref',key,label:ref,km:0});}
 function selectReview(item:ReviewItem){setSelectedReview(item.id);flyTo(item.at);}
 async function saveReview(item:ReviewItem,status:ReviewStatus,note:string){
  setReviewError('');
  try{const saved=await api<Partial<ReviewEntry>>(`/api/big-roads/review/${encodeURIComponent(item.key)}`,{method:'PUT',body:JSON.stringify({status,note})});if(mounted.current)setReview(r=>({...r,[item.key]:{status:saved?.status??status,note:saved?.note??note,updated_at:saved?.updated_at}}));return true;}
  catch(e){if(mounted.current)setReviewError(stringError(e));return false;}
 }
 function pickTrial(at:LatLon){const which=trialPicking;if(!which)return;setTrialPoints(p=>({...p,[which]:at}));setTrialPicking(null);}
 function onRoadClick(feature:Feature,at:LatLon){if(trialPicking){pickTrial(at);return;}setDetail(feature);openDrawer('review');}
 function onJunctionClick(feature:Feature,at:LatLon){
  if(trialPicking){pickTrial(at);return;}
  const p=feature.properties||{};if(p.type!=='deadend'||p.node_id==null)return;
  const id=`deadend:${p.node_id}`,item=allItems.find(i=>i.id===id);if(!item)return;
  if(!filterReview([item],reviewFilter).length)setReviewFilter({all:true,reason:'',status:''});
  setDetail(null);openDrawer('review');setSelectedReview(id);
 }
 function onMapClick(){setDetail(null);}
 function trialFromForm(){const from=pointFrom(origin),to=pointFrom(destination);if(!from||!to){setTrialError('Tọa độ trong form không hợp lệ.');return;}setTrialError('');setTrialPoints({origin:[from.lat,from.lon],destination:[to.lat,to.lon]});}
 async function runTrial(){
  if(!trialPoints.origin||!trialPoints.destination)return;
  setTrialBusy(true);setTrialError('');setTrial(null);
  try{const result=await api<TrialRoute>('/api/big-roads/route',{method:'POST',body:JSON.stringify({origin:trialPoints.origin,destination:trialPoints.destination})});if(mounted.current){setTrial(result);const bbox=trialBounds(result);if(bbox)setFocus({seq:++focusSeq.current,kind:'bounds',bbox});}}
  catch(e){if(mounted.current)setTrialError(stringError(e));}
  finally{if(mounted.current)setTrialBusy(false);}
 }
 function clearTrial(){setTrial(null);setTrialPoints({origin:null,destination:null});setTrialPicking(null);setTrialError('');}
 function setLayer(key:keyof LayerState,value:boolean){if(key==='basemap')setBasemap(value);else setLayers(l=>({...l,[key]:value}));}

 const busy=jobBusy||!!job&&['queued','running'].includes(job.status);
 const validOrigin=pointFrom(origin)||initialOrigin,validDestination=pointFrom(destination)||initialDestination;
 function selectPoint(point:Point){(picking==='origin'?setOrigin:setDestination)([String(point.lat),String(point.lon)]);setPicking(null);}
 const roads=options?.road_classes||[];
 const classStats=route?roads.map(road=>{const matching=route.segments.filter(s=>s.road_class===road.id);return{...road,km:matching.reduce((v,s)=>v+s.distance_m,0)/1000,minutes:matching.reduce((v,s)=>v+s.minutes,0)};}).filter(s=>s.km>0):[];
 const big=bigMode&&!!bigData;

 const planner=<div className="planner"><div className="panel-title"><div><span className="eyebrow">LẬP HÀNH TRÌNH</span><h2>Bạn muốn đi đâu?</h2></div><span className="square-icon"><RouteIcon size={20}/></span></div><form onSubmit={estimate} noValidate><div className="endpoint-group">{([{key:'origin',title:'Điểm đi',letter:'A',values:origin,set:setOrigin},{key:'destination',title:'Điểm đến',letter:'B',values:destination,set:setDestination}] as const).map(endpoint=><div className={`endpoint ${picking===endpoint.key?'is-picking':''}`} key={endpoint.key}><div className="endpoint-top"><span className={`point-letter ${endpoint.letter==='B'?'orange':''}`}>{endpoint.letter}</span><label>{endpoint.title}</label><button type="button" className="map-pick" aria-label={`Chọn ${endpoint.title.toLowerCase()} trên bản đồ`} onClick={()=>setPicking(picking===endpoint.key?null:endpoint.key)}><LocateFixed size={14}/>{picking===endpoint.key?'Đang chọn…':'Chọn bản đồ'}</button></div><div className="coordinate-fields"><label>Vĩ độ<input aria-label={`Vĩ độ ${endpoint.title.toLowerCase()}`} inputMode="decimal" value={endpoint.values[0]} onChange={e=>endpoint.set([e.target.value,endpoint.values[1]])}/></label><label>Kinh độ<input aria-label={`Kinh độ ${endpoint.title.toLowerCase()}`} inputMode="decimal" value={endpoint.values[1]} onChange={e=>endpoint.set([endpoint.values[0],e.target.value])}/></label></div></div>)}<button type="button" className="swap-button" aria-label="Đổi điểm đi và điểm đến" onClick={()=>{setOrigin(destination);setDestination(origin);}}><ArrowDownUp size={15}/></button></div><div className="form-row"><label>Ngày trong tuần<select aria-label="Ngày trong tuần" value={day} onChange={e=>setDay(Number(e.target.value))}>{days.map((label,index)=><option key={label} value={index}>{label}</option>)}</select></label><label>Giờ khởi hành<input type="time" aria-label="Giờ khởi hành" required value={hour} onChange={e=>setHour(e.target.value)}/></label></div><label className="method-label">Phương pháp nội suy<select aria-label="Phương pháp nội suy" value={method} onChange={e=>setMethod(e.target.value)}>{options?.methods.map(m=><option value={m.id} key={m.id}>{m.label}</option>)}</select></label><div className="method-hint"><Layers3 size={15}/><span>Chỉ dùng các mẫu cùng loại đường để nội suy vận tốc.</span></div>{error&&<div className="error-box" role="alert"><TriangleAlert size={16}/><span>{error}</span></div>}<button type="submit" className="primary-button estimate-button" disabled={loading||!options}>{loading?<LoaderCircle className="spin" size={18}/>:<Navigation size={17}/>}<span>{loading?'Đang tính hành trình…':'Ước lượng hành trình'}</span>{!loading&&<ArrowRight size={18}/>}</button></form><div className="planner-foot"><span className="tiny-dot"/>Dữ liệu thực · Định tuyến OSRM</div></div>;
 const notes=<div className="big-road-notes">
  {bigLoading&&<span className="big-road-status">Đang tải bản đồ đường lớn…</span>}
  {bigError&&<div className="error-box big-road-error" role="alert"><TriangleAlert size={16}/><span>{bigError}</span></div>}
  {big&&!bigData!.land&&<div className="big-road-status"><TriangleAlert size={14}/>Chưa có hình đất liền; đang dùng ảnh nền.</div>}
  {big&&!localZoom&&(showB||showC)&&<div className="big-road-status">Phóng to để xem điểm giao B/C</div>}
  {big&&noGroups&&<div className="big-road-status">Đã tắt mọi nhóm đường</div>}
  {big&&bigData!.meta.osrm_bridging&&bigData!.meta.osrm_bridging!=='ok'&&<div className="big-road-status"><TriangleAlert size={14}/>{`Chưa nối bù qua OSRM: ${bigData!.meta.osrm_bridging.replace(/^skipped: /,'')}`}</div>}
  {big&&clusterOn&&clustersOutdated(bigData!.clusters,bigData!.meta)&&<div className="big-road-status warning"><TriangleAlert size={14}/>Dữ liệu cụm dựng từ bản đồ cũ</div>}
  {big&&health?.big_roads_current===false&&<div className="big-road-status warning"><TriangleAlert size={14}/>Bản đồ đường lớn dựng từ bản OSM cũ hơn. Chạy lại python model/build_big_roads.py.</div>}
 </div>;
 const resultPanel=<section className={`result-panel ${route?'has-route':''}`} aria-live="polite">{route?<><div className="journey-summary"><div className="result-check"><Check size={19}/></div><div><span className="eyebrow">HÀNH TRÌNH DỰ KIẾN</span><h2>A <ArrowRight size={16}/> B <span>{days[day]} · {hour}</span></h2></div><div className="journey-metric"><strong>{number(route.minutes)}</strong><span>phút di chuyển</span></div><div className="journey-metric"><strong>{number(route.km)}</strong><span>km quãng đường</span></div><span className="result-method">{options?.methods.find(m=>m.id===route.method)?.label||route.method}</span></div>{classStats.length>0&&<div className="class-breakdown">{classStats.map(stat=><div key={stat.id}><span><i style={{background:stat.color}}/>{stat.label}</span><b>{number(stat.km)} km <small>· {number(stat.minutes)} phút</small></b></div>)}</div>}{route.warnings?.length>0&&<div className="route-warnings">{route.warnings.map((warning,index)=><span key={index}><TriangleAlert size={15}/>{warning}</span>)}</div>}</>:<><span className="result-empty-icon"><Clock3 size={25}/></span><div><h3>Hành trình của bạn sẽ hiển thị ở đây</h3><p>Chọn hai điểm và thời gian khởi hành để xem ước lượng theo từng loại đường.</p></div><span className="result-placeholder">— <small>phút</small></span></>}</section>;
 // Spec 20: the drawer beside the map, one scrolling panel per tab; inactive panels stay mounted but hidden.
 const tabs:[string,string][]=big?BIG_TABS:[['route','Hành trình']],activeTab=big?drawer.tab:'route';
 const tabPanel=(id:string,content:React.ReactNode)=><div className="drawer-panel" role="tabpanel" key={id} id={`drawer-panel-${id}`} aria-labelledby={`drawer-tab-${id}`} hidden={activeTab!==id}>{content}</div>;
 const drawerPanel=<aside id="drawer" className="drawer" aria-label="Ngăn kéo" hidden={!drawer.open}>
  {notes}
  {big&&routeHit&&selection&&<RouteSummary hit={routeHit} stats={selection} onClear={clearRoute}/>}
  <div className="drawer-tabs" role="tablist" aria-label="Thẻ ngăn kéo">{tabs.map(([id,label])=><button type="button" role="tab" key={id} id={`drawer-tab-${id}`} aria-controls={`drawer-panel-${id}`} aria-selected={activeTab===id} className={activeTab===id?'selected':''} onClick={()=>{if(big)setDrawer(d=>({...d,tab:id as BigTab}));}}>{label}</button>)}</div>
  {big?<>
   {tabPanel('layers',<BigToolbar query={query} onQuery={setQuery} suggestions={suggestions} selected={routeHit} onSelectRoute={selectRoute} onClearRoute={clearRoute} groups={groups} onToggleGroup={g=>setGroups(s=>({...s,[g]:!s[g]}))} colorBy={colorBy} onColorBy={setColorBy} layers={{...layers,basemap}} onLayer={setLayer} clusters={bigData!.clusters??null} hasProvinces={!!bigData!.provinces}/>)}
   {tabPanel('clusters',partition&&bigData?.clusters?<>
    <div className="side-panel"><PartitionPicker ks={availableKs(bigData.clusters)} k={partition.k} onK={setPartChoice} provinceK={province?.k??null} provinceOn={partition.method==='province'} onProvince={()=>setPartChoice('province')} naturalK={naturalPartition(bigData.clusters)?.k??null} naturalOn={partition.method==='natural'} onNatural={()=>setPartChoice('natural')} twoLevelK={twoLevelPartition(bigData.clusters)?.k??null} twoLevelOn={partition.method==='natural_spectral'} onTwoLevel={()=>setPartChoice('natural_spectral')}/></div>
    <ClusterPanel clusters={bigData.clusters} partition={partition}/>
    {spectral32&&province&&<ComparePanel spectral={spectral32} province={province}/>}
   </>:<p className="side-empty drawer-empty">Bật lớp Cụm trong thẻ Lớp để chọn cách chia.</p>)}
   {tabPanel('review',detail?<SegmentDetail feature={detail} onClose={()=>setDetail(null)} onPaintRoute={paintRoute}/>:<ReviewPanel items={reviewList} filter={reviewFilter} onFilter={setReviewFilter} selectedId={selectedReview} onSelect={selectReview} onSave={saveReview} error={reviewError} loadError={reviewLoadError}/>)}
   {tabPanel('trial',<TrialPanel points={trialPoints} picking={trialPicking} onPick={setTrialPicking} onFromForm={trialFromForm} onRun={()=>void runTrial()} onClear={clearTrial} busy={trialBusy} error={trialError} trial={trial}/>)}
   {tabPanel('stats',<BigStats meta={bigData!.meta} junctions={bigMainland!.junctions}/>)}
  </>:tabPanel('route',<>{planner}<div className="drawer-row"><button type="button" className={`basemap-toggle ${basemap?'active':''}`} aria-pressed={basemap} onClick={()=>setBasemap(!basemap)}><MapPin size={14}/>Ảnh nền</button></div>{resultPanel}</>)}
 </aside>;
 const legend=big?<BigLegend colorBy={clusterOn?'cluster':colorBy} partition={partition} basemap={basemap}/>:<div className="map-legend"><span className="legend-title">LOẠI ĐƯỜNG</span>{roads.map(road=><span key={road.id}><i style={{background:road.color}}/>{road.label}</span>)}{basemap&&<span className="map-caption">Bản đồ nền cần kết nối mạng</span>}</div>;
 // Over the map only: Leaflet zoom, the top-right toggles, the legend (collapsible to a chip) and the trip summary.
 const mapPanel=<div className="map-panel">
  <TrafficMap origin={validOrigin} destination={validDestination} route={route} classes={roads} picking={bigMode?trialPicking:picking} onPick={bigMode?(p:Point)=>pickTrial([p.lat,p.lon]):selectPoint} bigRoads={bigView} basemap={basemap} onZoomChange={setZoom} onRoadClick={onRoadClick} onJunctionClick={onJunctionClick} onMapClick={onMapClick} layoutKey={`${drawer.open}`}/>
  <div className="map-top-right"><div className="map-toggles">
   <button type="button" className={`big-road-toggle ${bigMode?'active':''}`} aria-pressed={bigMode} disabled={bigLoading} onClick={()=>void toggleBigRoads()}>{bigLoading?<LoaderCircle className="spin" size={14}/>:<Layers3 size={14}/>}Chỉ đường lớn</button>
   <button type="button" className="basemap-toggle side-toggle" aria-label={drawer.open?'Thu gọn cột phụ':'Mở cột phụ'} title="[" aria-expanded={drawer.open} aria-controls="drawer" onClick={toggleDrawer}>{drawer.open?<PanelLeftClose size={14}/>:<PanelLeftOpen size={14}/>}</button>
  </div></div>
  {picking&&!bigMode&&<div className="pick-banner"><LocateFixed size={17}/>Nhấp bản đồ để chọn {picking==='origin'?'điểm đi A':'điểm đến B'}<button aria-label="Hủy chọn điểm" onClick={()=>setPicking(null)}><X size={16}/></button></div>}
  {trialPicking&&bigMode&&<div className="pick-banner"><LocateFixed size={17}/>Nhấp bản đồ để chọn {trialPicking==='origin'?'điểm đi thử A':'điểm đến thử B'}<button aria-label="Hủy chọn điểm" onClick={()=>setTrialPicking(null)}><X size={16}/></button></div>}
  <div className="map-legend-dock">{legendOpen?<div className="legend-box">{legend}<button type="button" className="icon-button legend-close" aria-label="Thu gọn chú giải" onClick={()=>setLegendOpen(false)}><X size={13}/></button></div>:<button type="button" className="legend-chip" aria-expanded={false} onClick={()=>setLegendOpen(true)}><Layers3 size={13}/>Chú giải</button>}</div>
  {!bigMode&&route&&<div className="map-summary" role="group" aria-label="Tóm tắt hành trình"><Clock3 size={16}/><div><b>{`${number(route.minutes)} phút`}</b><span>{`${number(route.km)} km`}</span></div></div>}
 </div>;

 // Spec 20: on the journey tab the page is the map: a 48px topbar, then the drawer and the map, no page scroll.
 return <div className={`app-shell ${pane==='route'?'route-view':''}`}><aside className="rail"><a className="brand-mark" href="#" aria-label="Hà Nội Traffic Lab"><Waypoints size={25}/></a><span className="rail-line"/><button aria-label="Ước lượng tuyến đường" className={pane==='route'?'rail-button active':'rail-button'} onClick={()=>setPane('route')}><RouteIcon size={21}/></button><button aria-label="Dữ liệu và kiểm định" className={pane==='data'?'rail-button active':'rail-button'} onClick={()=>setPane('data')}><Database size={21}/></button>{pane==='route'&&bigMode&&<button type="button" className="rail-button planner-rail-button" aria-label="Lập hành trình" title="Lập hành trình" onClick={()=>{leaveBigMode();openDrawer();}}><Navigation size={19}/></button>}<div className="rail-bottom">HN</div></aside><div className="main-shell"><header className="topbar"><div className="wordmark">HÀ NỘI <span>/</span> <b>TRAFFIC LAB</b><small>MÔ HÌNH GIAO THÔNG</small></div><div className="workspace-tabs" role="tablist" aria-label="Không gian làm việc"><button role="tab" aria-selected={pane==='route'} className={pane==='route'?'selected':''} onClick={()=>setPane('route')}><Navigation size={16}/>Ước lượng hành trình</button><button role="tab" aria-selected={pane==='data'} className={pane==='data'?'selected':''} onClick={()=>setPane('data')}><FlaskConical size={16}/>Dữ liệu & kiểm định</button><span className="workspace-note"><span/>Mô hình theo loại đường</span></div><div className="top-right">{pane==='route'&&<span className="top-data"><Database size={13}/><b>{data?number(data.sample_count,0):'—'}</b> mẫu vận tốc · <b>{roads.length||'—'}</b> loại đường</span>}<span className={`connection ${health?.osrm_ready?'online':''}`}><i/>{health?.osrm_ready?'OSRM đang hoạt động':'OSRM chưa sẵn sàng'}</span><span className="local-tag">LOCAL WORKSPACE</span></div></header>
 {pane==='route'?<main className="route-main"><section className={`route-layout ${drawer.open?'drawer-open':''}`}>{drawerPanel}{mapPanel}</section></main>
 :<main><section className="page-heading"><div><div className="eyebrow">KHÔNG GIAN PHÂN TÍCH <span>01 / HÀ NỘI</span></div><h1>Hiểu đường. <em>Ước lượng đúng.</em></h1><p>Vận tốc được nội suy riêng cho từng loại đường và thời điểm di chuyển.</p></div><div className="data-chip"><span className="chip-icon"><Database size={19}/></span><div><strong>{data?number(data.sample_count,0):'—'}</strong><span>mẫu vận tốc</span></div><span className="chip-separator"/><div><strong>{roads.length||'—'}</strong><span>loại đường</span></div></div></section><section className="data-workspace"><div className="data-panel"><div className="panel-title"><div><span className="eyebrow">ĐỘ PHỦ DỮ LIỆU</span><h2>Mẫu theo loại đường</h2></div><Database size={22}/></div><p className="muted">Các loại đường được kiểm tra và nội suy độc lập.</p><div className="coverage-bars">{roads.map(road=>{const count=data?.by_class?.[road.id]||0;return <div className="coverage-row" key={road.id}><div><span><i style={{background:road.color}}/>{road.label}</span><b>{number(count,0)} <small>mẫu</small></b></div><div className="bar-track"><span style={{background:road.color,width:`${Math.max(count?2:0,count/Math.max(1,data?.sample_count||1)*100)}%`}}/></div>{!count&&<small className="coverage-warning">Chưa có mẫu cho loại đường này</small>}</div>;})}</div><p className="small-note">Cập nhật: {data?.last_updated?new Date(data.last_updated).toLocaleString('vi-VN'):'Chưa có thời điểm cập nhật'}</p><div className="service-status"><span><i className={health?.api_key_configured?'good':''}/>{health?.api_key_configured?'TomTom đã cấu hình':'TomTom chưa cấu hình khóa'}</span><span><i className={health?.samples_ready?'good':''}/>{health?.samples_ready?'Dữ liệu sẵn sàng':'Dữ liệu cần bổ sung'}</span></div></div><div className="data-panel"><div className="panel-title"><div><span className="eyebrow">BỔ SUNG DỮ LIỆU</span><h2>Thu thập có giới hạn</h2></div><ArrowUpRight size={23}/></div><p className="muted">Khóa API được giữ trên máy chủ. Mỗi tác vụ bị giới hạn số lượt gọi.</p><div className="crawl-fields"><label>Ngày thu thập<input type="date" aria-label="Ngày thu thập" value={crawlDate} onChange={e=>setCrawlDate(e.target.value)}/></label><label>Khung giờ (HHMM, cách nhau bằng dấu phẩy)<input aria-label="Khung giờ thu thập" value={slots} onChange={e=>setSlots(e.target.value)} placeholder="0800,1730"/></label><label>Số lượt gọi tối đa<input type="number" aria-label="Số lượt gọi tối đa" min={1} max={60} value={maxCalls} onChange={e=>setMaxCalls(Number(e.target.value))}/></label></div><button className="primary-button" disabled={busy||!health?.api_key_configured} onClick={()=>void startJob('crawl')}>{busy?<LoaderCircle className="spin" size={17}/>:<Database size={17}/>}Thu thập dữ liệu<ArrowRight size={17}/></button>{jobError&&<div className="error-box" role="alert"><TriangleAlert size={16}/>{jobError}</div>}{job&&<div className={`job-status ${job.status==='failed'?'failed':''}`} role="status"><span>{['queued','running'].includes(job.status)?<LoaderCircle className="spin" size={17}/>:job.status==='completed'?<Check size={17}/>:<TriangleAlert size={17}/>}<b>{statusLabels[job.status]||job.status}</b></span><small>Tác vụ {job.id.slice(0,12)}</small>{job.error&&<p>{job.error}</p>}{job.status==='completed'&&job.result!=null&&<details><summary>Xem kết quả tác vụ</summary><pre>{JSON.stringify(job.result,null,2)}</pre></details>}</div>}</div><div className="data-panel experiment-panel"><div className="experiment-heading"><div><span className="eyebrow">SO SÁNH CÔNG THỨC</span><h2>Để dữ liệu lựa chọn phương pháp.</h2><p className="muted">Đánh giá giữ lại mẫu theo từng loại đường. Sai số nhỏ hơn là tốt hơn.</p></div><button className="secondary-button" disabled={busy||!data?.sample_count} onClick={()=>void startJob('experiments')}><FlaskConical size={17}/>Chạy kiểm định<ChevronRight size={16}/></button></div>{experiments?.scores&&Object.values(experiments.scores).some(v=>Object.keys(v).length)?<div className="table-scroll"><table><thead><tr><th>Loại đường</th><th>Phương pháp</th><th>Mẫu kiểm định</th><th>MAE (km/h)</th><th>MAE có trọng số</th><th>Sai số thời gian (%)</th></tr></thead><tbody>{Object.entries(experiments.scores).flatMap(([road,methods])=>Object.entries(methods).map(([id,score])=><tr key={`${road}-${id}`}><td>{roads.find(r=>r.id===road)?.label||classLabels[road]||road}</td><td>{options?.methods.find(m=>m.id===id)?.label||id}</td><td>{score.n_held_out}</td><td>{score.mae_kmh==null?'—':number(score.mae_kmh,2)}</td><td>{score.length_weighted_mae_kmh==null?'—':number(score.length_weighted_mae_kmh,2)}</td><td>{score.travel_time_wape_pct==null?'—':number(score.travel_time_wape_pct,2)}</td></tr>))}</tbody></table></div>:<div className="empty-experiments"><FlaskConical size={30}/><strong>Chưa có kết quả kiểm định</strong><span>Chạy kiểm định để so sánh các công thức trên dữ liệu hiện có.</span></div>}{experiments?.limitations?.map((limitation,index)=><p key={index} className="small-note">{limitation}</p>)}</div></section>
 <footer><span>HÀ NỘI TRAFFIC LAB <i/>Không gian nghiên cứu giao thông</span><span>Ước lượng từ dữ liệu mẫu · Chưa thay thế thời gian di chuyển thực tế</span></footer></main>}</div></div>;
}
