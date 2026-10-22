// Big-road (network A) review tools: toolbar, stat cards, legend and drawer panels.
// Every OSM-derived string is rendered as a React text node, never as HTML.
import {useEffect,useId,useRef,useState} from 'react';
import type {Feature} from 'geojson';
import {Ban,ChevronDown,CircleDot,Download,ExternalLink,Layers3,LoaderCircle,LocateFixed,Pencil,Route as RouteIcon,Search,Ship,TriangleAlert,X} from 'lucide-react';
import type {BigRoadMeta,BigRoadClusters,ClusterPartition,ReviewStatus,TrialRoute} from './api';
import {GROUPS,type Group,groupLabels,bridgeKindLabels,speedSourceLabels,reviewStatusLabels,deadEndReasons,SPEED_BANDS,UNKNOWN_COLOR,lineKm,refParts,ferryTipLines,clusterStatRows,partitionLabel,compareRows,reviewCounts,reviewCsv,reviewReasonText,osmViewUrl,osmEditUrl,type ReviewItem,type ReviewFilter,type RouteHit,type RouteStats} from './bigRoads';

export const number=(n:number,digits=1)=>n.toLocaleString('vi-VN',{maximumFractionDigits:digits});
const props=(f:Feature)=>(f.properties||{}) as Record<string,any>;
const OsmLink=({href,label,icon}:{href:string;label:string;icon?:React.ReactNode})=><a className="osm-link" href={href} target="_blank" rel="noopener noreferrer">{icon}{label}</a>;

export interface LayerState{showB:boolean;showC:boolean;showBorder:boolean;showDead:boolean;showFerries:boolean;showClusters:boolean;showProvinces:boolean;basemap:boolean}
interface ToolbarProps{
 query:string;onQuery:(q:string)=>void;suggestions:RouteHit[];selected:RouteHit|null;onSelectRoute:(hit:RouteHit)=>void;onClearRoute:()=>void;
 groups:Record<Group,boolean>;onToggleGroup:(g:Group)=>void;
 colorBy:'group'|'speed';onColorBy:(c:'group'|'speed')=>void;
 layers:LayerState;onLayer:(key:keyof LayerState,value:boolean)=>void;clusters:BigRoadClusters|null;
 // Shown only while the cluster layer is on (spec 18.14).
 ks?:number[];k?:number|null;onK?:(k:number)=>void;
 // Spec 19.6: the province partition button and the province outline layer.
 provinceK?:number|null;provinceOn?:boolean;onProvince?:()=>void;hasProvinces?:boolean;
}
export function BigToolbar(p:ToolbarProps){
 const [menu,setMenu]=useState(false);const listId=useId(),menuRef=useRef<HTMLDivElement>(null);
 useEffect(()=>{if(!menu)return;const close=(e:MouseEvent)=>{if(!menuRef.current?.contains(e.target as Node))setMenu(false);};document.addEventListener('mousedown',close);return()=>document.removeEventListener('mousedown',close);},[menu]);
 const open=!!p.query.trim()&&(!p.selected||p.query!==p.selected.label);
 const layer=(key:keyof LayerState,label:string,icon?:React.ReactNode)=><label className="layer-option"><input type="checkbox" checked={p.layers[key]} onChange={e=>p.onLayer(key,e.target.checked)}/>{icon}{label}</label>;
 return <div className="big-toolbar" role="toolbar" aria-label="Công cụ bản đồ đường lớn">
  <div className="route-search">
   <Search size={14}/>
   <input role="combobox" aria-label="Tìm tuyến" aria-expanded={open} aria-controls={listId} aria-autocomplete="list" placeholder="Tìm tuyến: QL.1, ĐT.915, tên đường" value={p.query}
    onChange={e=>p.onQuery(e.target.value)}
    onKeyDown={e=>{if(e.key==='Escape'){e.preventDefault();p.onClearRoute();}else if(e.key==='Enter'&&open&&p.suggestions[0]){e.preventDefault();p.onSelectRoute(p.suggestions[0]);}}}/>
   {(p.query||p.selected)&&<button type="button" className="icon-button" aria-label="Bỏ chọn tuyến" onClick={p.onClearRoute}><X size={14}/></button>}
   {open&&<ul className="route-suggestions" role="listbox" id={listId}>{p.suggestions.length?p.suggestions.map(hit=><li key={hit.id} role="option" aria-selected={false} onClick={()=>p.onSelectRoute(hit)}><b>{hit.label}</b><span>{number(hit.km)} km</span></li>):<li className="empty" role="option" aria-selected={false} aria-disabled="true">Không có tuyến khớp</li>}</ul>}
  </div>
  <div className="group-chips">{GROUPS.map(g=><button type="button" key={g} className={`chip chip-${g} ${p.groups[g]?'on':''}`} aria-pressed={p.groups[g]} onClick={()=>p.onToggleGroup(g)}><i/>{groupLabels[g]}</button>)}</div>
  <div className="color-by" role="group" aria-label="Màu theo"><span>Màu theo</span>{(['group','speed'] as const).map(c=><button type="button" key={c} aria-pressed={p.colorBy===c} disabled={p.layers.showClusters} className={p.colorBy===c?'on':''} onClick={()=>p.onColorBy(c)}>{c==='group'?'Loại đường':'Vận tốc'}</button>)}</div>
  <div className="layer-menu" ref={menuRef}>
   <button type="button" className="layer-button" aria-expanded={menu} aria-haspopup="true" onClick={()=>setMenu(!menu)}><Layers3 size={14}/>Lớp<ChevronDown size={13}/></button>
   {menu&&<div className="layer-popup" role="group" aria-label="Lớp hiển thị" onKeyDown={e=>{if(e.key==='Escape')setMenu(false);}}>
    {layer('showB','Giao với B',<i className="dot" style={{background:'#3974d8'}}/>)}
    {layer('showC','Giao với C',<i className="dot" style={{background:'#2a9d8f'}}/>)}
    {layer('showBorder','Giao biên giới',<i className="square"/>)}
    {layer('showDead','Điểm cụt',<i className="ring"/>)}
    {layer('showFerries','Phà',<i className="ferry"/>)}
    {p.clusters?layer('showClusters','Cụm'):<span className="layer-option disabled">Cụm: chưa có dữ liệu</span>}
    {p.hasProvinces?layer('showProvinces','Ranh giới tỉnh',<i className="province"/>):<span className="layer-option disabled">Ranh giới tỉnh: chưa có dữ liệu</span>}
    {layer('basemap','Ảnh nền')}
   </div>}
  </div>
  {p.ks&&<PartitionPicker ks={p.ks} k={p.k} onK={p.onK} provinceK={p.provinceK} provinceOn={p.provinceOn} onProvince={p.onProvince}/>}
 </div>;
}
// The "Cách chia" buttons; spec 20 shows them in the drawer's "Cụm" tab.
export function PartitionPicker(p:{ks:number[];k?:number|null;onK?:(k:number)=>void;provinceK?:number|null;provinceOn?:boolean;onProvince?:()=>void;naturalK?:number|null;naturalOn?:boolean;onNatural?:()=>void;twoLevelK?:number|null;twoLevelOn?:boolean;onTwoLevel?:()=>void}){
 if(!p.ks.length&&p.provinceK==null&&p.naturalK==null&&p.twoLevelK==null)return null;
 return <div className="color-by k-picker" role="group" aria-label="Cách chia"><span>Cách chia</span>{p.ks.map(k=>{const on=!p.provinceOn&&!p.naturalOn&&!p.twoLevelOn&&p.k===k;return <button type="button" key={k} aria-pressed={on} className={on?'on':''} onClick={()=>p.onK?.(k)}>{`k = ${k}`}</button>;})}{p.provinceK!=null&&<button type="button" aria-pressed={!!p.provinceOn} className={p.provinceOn?'on':''} onClick={()=>p.onProvince?.()}>{`Tỉnh (${p.provinceK})`}</button>}{p.naturalK!=null&&<button type="button" aria-pressed={!!p.naturalOn} className={p.naturalOn?'on':''} onClick={()=>p.onNatural?.()}>{`Sông (${p.naturalK})`}</button>}{p.twoLevelK!=null&&<button type="button" aria-pressed={!!p.twoLevelOn} className={p.twoLevelOn?'on':''} onClick={()=>p.onTwoLevel?.()}>{`Sông + spectral (${p.twoLevelK})`}</button>}</div>;
}

export function BigStats({meta,junctions}:{meta:BigRoadMeta;junctions:Feature[]}){
 const km=meta.km,total=km.caotoc+km.quoclo+(km.tinhlo??0);
 const dead=junctions.filter(f=>props(f).type==='deadend');const mainline=dead.filter(f=>props(f).mainline).length;
 const hidden=[meta.island_km?`đã ẩn ${number(meta.island_km)} km trên đảo`:'',meta.fragments?`đã ẩn ${number(meta.fragments,0)} mảnh ngắn`:''].filter(Boolean).join(' · ');
 const card=(label:string,value:string,sub?:string)=><li className="stat-card"><span>{label}</span><b>{value}</b>{sub&&<small>{sub}</small>}</li>;
 return <ul className="big-stats" aria-label="Số liệu mạng A">
  {card('Tổng chiều dài',`${number(total,0)} km`,hidden||undefined)}
  {card('Theo nhóm',`${number(km.caotoc,0)} · ${number(km.quoclo,0)} · ${number(km.tinhlo??0,0)} km`,'cao tốc · quốc lộ · tỉnh lộ')}
  {card('Điểm cụt',`${number(mainline,0)} / ${number(meta.junctions.deadend??dead.length,0)}`,'tuyến chính / tất cả')}
  {card('Phà',`${number(meta.ferries??0,0)} tuyến`,meta.ferries_unconfirmed?`${number(meta.ferries_unconfirmed,0)} chưa ghi chở ô tô`:undefined)}
  {card('Nối bù',`${number(meta.bridge_km??0)} km`)}
  {card('Số khúc',number(meta.components,0))}
  {card('Điểm giao',`${number(meta.junctions.B,0)} B · ${number(meta.junctions.C,0)} C · ${number(meta.junctions.border??0,0)} biên giới`)}
 </ul>;
}

export function BigLegend({colorBy,partition,basemap}:{colorBy:'group'|'speed'|'cluster';partition:ClusterPartition|null;basemap:boolean}){
 return <div className="map-legend big-road-legend">
  {colorBy==='speed'?<><span className="legend-title">VẬN TỐC</span>{SPEED_BANDS.map((b,i)=><span key={b.color}><i style={{background:b.color}}/>{i===SPEED_BANDS.length-1?`${b.label} km/h`:b.label}</span>)}<span><i style={{background:UNKNOWN_COLOR}}/>Không rõ</span><span className="legend-note">Vận tốc tự do theo profile OSRM, chưa tính kẹt xe</span></>
  :colorBy==='cluster'&&partition?<><span className="legend-title">CỤM</span><span>{partition.method==='province'?`Tô theo tỉnh · ${partition.k} tỉnh`:partition.method==='natural'?`Tô theo sông · ${partition.k} vùng`:partition.method==='natural_spectral'?`Tô theo sông + spectral · ${partition.k} vùng`:`Tô theo cụm phổ · k = ${partition.k}`}</span><span><i className="cut"/>Nhát cắt</span><span><i className="dot" style={{background:'#000000'}}/>Nút biên</span></>
  :<><span className="legend-title">ĐƯỜNG LỚN</span><span><i style={{background:'#dc3e48'}}/>Cao tốc</span><span><i style={{background:'#e89b36'}}/>Quốc lộ</span><span><i style={{background:'#d4b12f'}}/>Đường tỉnh</span><span><i style={{background:'#8a949a'}}/>Khúc cô lập</span></>}
  <span><i className="dashed"/>Chỉ nhờ số hiệu</span><span><i className="dotted"/>Nối bù</span><span><i className="ferry"/>Phà</span>
  <span><i className="dot" style={{background:'#3974d8'}}/>Giao B</span><span><i className="dot" style={{background:'#2a9d8f'}}/>Giao C</span><span><i className="square"/>Giao biên giới</span><span><i className="ring"/>Điểm cụt</span>
  {basemap&&<span className="map-caption">Bản đồ nền cần kết nối mạng</span>}
 </div>;
}

const kindIcon=(item:ReviewItem)=>item.kind==='ferry'?<Ship size={14} aria-label="Phà"/>:item.kind==='osrm'?<TriangleAlert size={14} aria-label="Lỗi OSRM"/>:<CircleDot size={14} aria-label="Điểm cụt"/>;
const MAX_ROWS=300;
interface ReviewProps{items:ReviewItem[];filter:ReviewFilter;onFilter:(f:ReviewFilter)=>void;selectedId:string|null;onSelect:(item:ReviewItem)=>void;onSave:(item:ReviewItem,status:ReviewStatus,note:string)=>Promise<boolean>;error:string;loadError:string}
export function ReviewPanel({items,filter,onFilter,selectedId,onSelect,onSave,error,loadError}:ReviewProps){
 // `items` already honours the kind and reason filters; the count ignores the status filter so
 // "còn N / tổng M" keeps its meaning while filtering by status.
 const counts=reviewCounts(items);
 const shown=items.filter(i=>!filter.status||i.status===filter.status);
 const selected=shown.find(i=>i.id===selectedId)||null;
 const [draft,setDraft]=useState('');
 useEffect(()=>{setDraft(selected?.note||'');},[selected?.id,selected?.note]);
 function exportCsv(){
  if(typeof URL.createObjectURL!=='function')return;
  const url=URL.createObjectURL(new Blob([reviewCsv(shown)],{type:'text/csv;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download='viec-can-ra-mang-a.csv';document.body.appendChild(a);a.click();a.remove();
  setTimeout(()=>URL.revokeObjectURL?.(url),0);
 }
 return <section className="side-panel review-panel" aria-label="Việc cần rà">
  <div className="side-heading"><h3>Việc cần rà</h3><span className="review-count">{`còn ${number(counts.left,0)} / tổng ${number(counts.total,0)}`}</span></div>
  <div className="review-filters">
   <label className="switch"><input type="checkbox" checked={filter.all} onChange={e=>onFilter({...filter,all:e.target.checked})}/>Tất cả điểm cụt</label>
   <select aria-label="Lọc theo lý do" value={filter.reason} onChange={e=>onFilter({...filter,reason:e.target.value})}><option value="">Mọi loại mục</option>{Object.entries(deadEndReasons).map(([id,label])=><option key={id} value={id}>{`Điểm cụt: ${label}`}</option>)}</select>
   <select aria-label="Lọc theo trạng thái" value={filter.status} onChange={e=>onFilter({...filter,status:e.target.value})}><option value="">Mọi trạng thái</option>{Object.entries(reviewStatusLabels).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select>
   <button type="button" className="secondary-button small" onClick={exportCsv}><Download size={14}/>Xuất CSV</button>
  </div>
  {loadError&&<p className="side-note"><TriangleAlert size={13}/>{loadError}</p>}
  {error&&<div className="error-box" role="alert"><TriangleAlert size={14}/><span>{error}</span></div>}
  {shown.length?<ul className="review-list">{shown.slice(0,MAX_ROWS).map(item=>{const active=item.id===selectedId;return <li key={item.id} className={`review-row ${active?'active':''} status-${item.status}`} aria-current={active?'true':undefined}>
   <button type="button" className="review-main" onClick={()=>onSelect(item)}>{kindIcon(item)}<span className="review-text"><b>{item.route||(item.kind==='osrm'?`Node ${item.key}`:'—')}</b><small>{reviewReasonText(item)}</small></span></button>
   <select aria-label="Trạng thái" value={item.status} onChange={e=>void onSave(item,e.target.value as ReviewStatus,item.note)}>{Object.entries(reviewStatusLabels).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select>
   {active&&<div className="review-detail">
    <div className="osm-links"><OsmLink href={osmViewUrl(item)} label="Xem trên OSM" icon={<ExternalLink size={12}/>}/><OsmLink href={osmEditUrl(item)} label="Sửa trên OSM" icon={<Pencil size={12}/>}/></div>
    <label className="note-field">Ghi chú<input aria-label="Ghi chú" maxLength={500} value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();void onSave(item,item.status,draft);}}}/></label>
    <button type="button" className="secondary-button small" onClick={()=>void onSave(item,item.status,draft)}>Lưu ghi chú</button>
   </div>}
  </li>;})}</ul>:<p className="side-empty">Không có mục nào khớp bộ lọc.</p>}
  {shown.length>MAX_ROWS&&<p className="side-note">{`Đang hiện ${MAX_ROWS} / ${number(shown.length,0)} mục; lọc thêm để xem hết.`}</p>}
 </section>;
}

export function ClusterPanel({clusters,partition}:{clusters:BigRoadClusters;partition:ClusterPartition}){
 return <section className="side-panel cluster-panel" aria-label="Phân cụm">
  <div className="side-heading"><h3>{partitionLabel(partition)}</h3></div>
  <table className="cluster-stats"><tbody>{clusterStatRows(partition.stats||{},partition.k).map(([label,value])=><tr key={label}><th scope="row">{label}</th><td>{value}</td></tr>)}</tbody></table>
  <p className="side-note">{`${clusters.method} · ${clusters.created_at}`}</p>
 </section>;
}

// Spec 19.6: spectral k = 32 against the province partition, the better value in bold.
export function ComparePanel({spectral,province}:{spectral:ClusterPartition;province:ClusterPartition}){
 const cell=(text:string,better:boolean)=><td className={better?'better':undefined}>{better?<strong>{text}</strong>:text}</td>;
 return <section className="side-panel compare-panel" aria-label="So sánh cách chia">
  <div className="side-heading"><h3>So sánh cách chia</h3></div>
  <table className="cluster-stats compare-stats"><thead><tr><th scope="col"></th><th scope="col">{`Spectral k = ${spectral.k}`}</th><th scope="col">Tỉnh</th></tr></thead>
   <tbody>{compareRows(spectral,province).map(r=><tr key={r.label}><th scope="row">{r.label}</th>{cell(r.spectral,r.better==='spectral')}{cell(r.province,r.better==='province')}</tr>)}</tbody></table>
 </section>;
}

export function RouteSummary({hit,stats,onClear}:{hit:RouteHit;stats:RouteStats;onClear:()=>void}){
 return <section className="side-panel route-summary" aria-label="Tuyến đã chọn">
  <div className="side-heading"><h3><RouteIcon size={15}/>{hit.label}</h3><button type="button" className="icon-button" aria-label="Bỏ tô tuyến" onClick={onClear}><X size={14}/></button></div>
  <p>{`${number(stats.km)} km · ${number(stats.pieces,0)} khúc rời · ${number(stats.deadEnds,0)} điểm cụt`}</p>
 </section>;
}

export function SegmentDetail({feature,onClose,onPaintRoute}:{feature:Feature;onClose:()=>void;onPaintRoute:(ref:string)=>void}){
 const p=props(feature),coords=feature.geometry?.type==='LineString'?feature.geometry.coordinates:[];
 const ferry=p.bridge_kind==='ferry',firstRef=refParts(p.ref)[0];
 const rows:[string,string][]=[];
 if(p.ref)rows.push(['Số hiệu',String(p.ref)]);
 if(p.name)rows.push(['Tên',String(p.name)]);
 rows.push(['Nhóm',groupLabels[p.group as Group]||String(p.group||'—')]);
 if(p.highway)rows.push(['Thẻ highway',String(p.highway)]);
 rows.push(['Chiều dài',`${number(lineKm(coords),2)} km`]);
 if(p.bridge&&p.bridge_kind)rows.push(['Nối bù',bridgeKindLabels[p.bridge_kind]||String(p.bridge_kind)]);
 if(p.speed_kmh!=null)rows.push(['Vận tốc',`${number(Number(p.speed_kmh),0)} km/h · ${speedSourceLabels[p.speed_source]||String(p.speed_source||'')}`]);
 else rows.push(['Vận tốc','không rõ']);
 if(p.isolated)rows.push(['Ghi chú','Khúc bị cô lập']);
 return <section className="side-panel segment-detail" aria-label="Chi tiết đoạn">
  <div className="side-heading"><h3>{ferry?(p.ferry_name||'Phà'):(firstRef||p.name||`(${p.highway})`)}</h3><button type="button" className="icon-button" aria-label="Đóng chi tiết" onClick={onClose}><X size={14}/></button></div>
  {ferry&&<p className="ferry-lines">{ferryTipLines(p).join(' · ')}</p>}
  <dl>{rows.map(([k,v])=><div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}<div><dt>way_id</dt><dd>{String(p.way_id??'—')}{p.way_id!=null&&<> <OsmLink href={`https://www.openstreetmap.org/way/${encodeURIComponent(String(p.way_id))}`} label="Xem trên OSM" icon={<ExternalLink size={12}/>}/></>}</dd></div></dl>
  <button type="button" className="secondary-button small" disabled={!firstRef} onClick={()=>firstRef&&onPaintRoute(firstRef)}><RouteIcon size={14}/>Tô cả tuyến</button>
 </section>;
}

type LatLon=[number,number];
interface TrialProps{points:{origin:LatLon|null;destination:LatLon|null};picking:'origin'|'destination'|null;onPick:(which:'origin'|'destination'|null)=>void;onFromForm:()=>void;onRun:()=>void;onClear:()=>void;busy:boolean;error:string;trial:TrialRoute|null}
const fmtPoint=(p:LatLon|null)=>p?`${p[0].toFixed(4)}, ${p[1].toFixed(4)}`:'chưa chọn';
export function TrialPanel({points,picking,onPick,onFromForm,onRun,onClear,busy,error,trial}:TrialProps){
 const pick=(which:'origin'|'destination',label:string,letter:string)=><div className="trial-point"><span className={`point-letter ${letter==='B'?'orange':''}`}>{letter}</span><span>{fmtPoint(points[which])}</span><button type="button" className={`map-pick ${picking===which?'active':''}`} aria-label={label} aria-pressed={picking===which} onClick={()=>onPick(picking===which?null:which)}><LocateFixed size={13}/>{picking===which?'Đang chọn…':'Chọn trên bản đồ'}</button></div>;
 return <section className="side-panel trial-panel" aria-label="Thử tuyến">
  <div className="side-heading"><h3>Thử tuyến</h3>{(points.origin||points.destination||trial)&&<button type="button" className="icon-button" aria-label="Xóa tuyến thử" onClick={onClear}><Ban size={13}/></button>}</div>
  {pick('origin','Chọn điểm đi','A')}{pick('destination','Chọn điểm đến','B')}
  <div className="trial-actions"><button type="button" className="secondary-button small" onClick={onFromForm}>Lấy từ form</button><button type="button" className="primary-button small" disabled={busy||!points.origin||!points.destination} onClick={onRun}>{busy&&<LoaderCircle className="spin" size={14}/>}Tìm tuyến</button></div>
  {error&&<div className="error-box" role="alert"><TriangleAlert size={14}/><span>{error}</span></div>}
  {trial&&<p className="trial-summary">{`${number(trial.km_on_a)} km trên mạng A, ${number(trial.km_off_a)} km ngoài mạng A, khoảng ${number(trial.minutes,0)} phút (OSRM, chưa tính kẹt xe)`}</p>}
  {trial&&<div className="trial-legend"><span><i className="trial-on"/>Trên mạng A</span><span><i className="trial-off"/>Ngoài mạng A</span></div>}
 </section>;
}
