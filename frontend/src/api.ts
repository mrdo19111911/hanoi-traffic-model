import type {Feature,FeatureCollection} from 'geojson';
export interface Point {lat:number;lon:number}
export interface RoadClass {id:string;label:string;color:string}
export interface Method {id:string;label:string}
export interface Options {road_classes:RoadClass[];methods:Method[];default_method:string}
export interface Health {status:string;osrm_ready:boolean;api_key_configured:boolean;samples_ready:boolean;big_roads_current?:boolean}
export interface BigRoadMeta {ways:number;km:{caotoc:number;quoclo:number;tinhlo?:number};junctions:{B:number;C:number;border?:number;deadend?:number};components:number;island_km?:number;bridges?:number;bridge_km?:number;fragments?:number;osrm_bridging?:string;osrm_bridges?:number;osrm_errors?:number;osrm_unmatched?:number;bridge_km_by_kind?:{road:number;small:number;ferry:number};ferries?:number;ferries_unconfirmed?:number;dead_end_reasons?:{construction:number;continues:number;end:number};osrm_error_samples?:{node_id:number|string;at:[number,number];error:string}[];[key:string]:unknown}
export type ReviewStatus='unseen'|'seen'|'skipped';
export interface ReviewEntry {status:ReviewStatus;note:string;updated_at?:string}
export interface BigRoadReview {items:Record<string,ReviewEntry>}
export interface ClusterStats {cut_edges:number;cut_km:number;cut_pct:number;boundary_nodes:number;contiguous:number|boolean;conductance_mean:number;km_min:number;km_median:number;km_max:number;unassigned_nodes?:number}
export interface ClusterPartition {method?:'spectral'|'province'|'natural'|'natural_spectral';parents?:Record<string,number>;adjacency?:number[][];extra_gates?:unknown[];rivers?:string[];names?:Record<string,string>;k:number;ways:Record<string,number>;cuts:number[][][];boundary:number[][];stats:ClusterStats}
// Spec 18.14: one partition per k in {8, 16, 32, 64}; replaces the single-k format of 18.10.
export interface BigRoadClusters {method:string;created_at:string;pbf_sha256?:string;policy_version?:number;partitions:ClusterPartition[]}
export interface TrialSegment {on_a:boolean;coords:number[][];km:number;seconds:number}
export interface TrialRoute {segments:TrialSegment[];km:number;minutes:number;km_on_a:number;km_off_a:number}
export interface BigRoads {roads:FeatureCollection;junctions:FeatureCollection;meta:BigRoadMeta;land:Feature|null;boundary?:Feature|null;clusters?:BigRoadClusters|null;provinces?:FeatureCollection|null}
export type ColorBy='group'|'speed'|'cluster';
export type MapFocus={seq:number;kind:'point';at:[number,number]}|{seq:number;kind:'bounds';bbox:[number,number,number,number]};
export interface TrialPoints {origin:[number,number]|null;destination:[number,number]|null}
export interface BigRoadView {roads:FeatureCollection;junctions:FeatureCollection;junctionKey:string;roadKey?:string;land:Feature|null;boundary?:Feature|null;compact?:boolean;colorBy?:ColorBy;clusters?:Record<string,number>|null;clusterColors?:Record<number,string>|null;cuts?:import('./bigRoads').CutInfo[]|null;boundaryNodes?:number[][]|null;cutKey?:string;provinces?:FeatureCollection|null;highlight?:Set<Feature>|null;focus?:MapFocus|null;trial?:TrialRoute|null;trialPoints?:TrialPoints|null}
export interface DataStatus {sample_count:number;by_class:Record<string,number>;by_slot:Record<string,number>;last_updated?:string;quality?:unknown}
export interface Geometry {type:'LineString';coordinates:number[][]}
export interface Segment {geometry:Geometry;road_class:string;speed_kmh:number;distance_m:number;minutes:number}
export interface Route {minutes:number;km:number;geometry:Geometry;segments:Segment[];by_class:Record<string,unknown>|unknown[];warnings:string[];method:string;data_quality?:unknown}
export interface Job {id:string;status:string;progress?:unknown;result?:unknown;error?:string;message?:string;[key:string]:unknown}
export interface Experiment {status:string;scores?:Record<string,Record<string,{n_held_out:number;mae_kmh:number;length_weighted_mae_kmh:number;travel_time_wape_pct:number;inside_triangle_pct?:number}>>;limitations?:string[]}
export async function api<T>(url:string,init:RequestInit={}):Promise<T>{
  const response=await fetch(url,{...init,headers:{'Content-Type':'application/json',...init.headers}});
  if(!response.ok){let message=`Máy chủ trả mã ${response.status}`;try{const body=await response.json();message=typeof body.detail==='string'?body.detail:body.error||body.message||message;}catch{}throw new Error(message);}
  return response.json();
}
