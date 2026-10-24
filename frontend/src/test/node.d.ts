// The few Node APIs the tests use to read the published building files; the project has no @types/node.
declare module 'node:fs'{export function readFileSync(path:string):Uint8Array;}
declare module 'node:path'{export function resolve(...paths:string[]):string;}
declare const process:{cwd():string};
