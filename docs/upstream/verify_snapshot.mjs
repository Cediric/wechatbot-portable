// Synthetic-only validation. No Electron, native DLL, database, device, or network.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {stripTypeScriptTypes} from 'node:module';
import {createHash} from 'node:crypto';

const root = path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'));
const patch = fs.readFileSync(path.join(decodeURIComponent(root), 'WeFlow-identity-snapshot.patch'), 'utf8');
function added(name) {
  const section = patch.split('--- a/electron/services/' + name + '\n')[1].split('--- a/')[0];
  return section.split('\n').filter(line => line.startsWith('+') && !line.startsWith('+++')).map(line => line.slice(1)).join('\n');
}
const chat = added('chatService.ts');
const method = chat.slice(chat.indexOf('  private identitySnapshotInFlight:'));
let timerId=0; const timers=new Map();
const context = {wcdbService: {}, createHash, rows: [], sessions: [], failContact: false, failSession: false, accountChanged: false,
  setTimeout(fn,ms){assert.equal(ms,2500);const id=++timerId;timers.set(id,fn);return id},clearTimeout(id){timers.delete(id)}};
context.wcdbService.execQuery = async (kind, dbPath, sql) => {
  assert.equal(kind, 'contact'); assert.equal(dbPath, null); assert.equal(sql, 'SELECT * FROM contact');
  return {success: !context.failContact, rows: context.rows};
};
context.wcdbService.getSessions = async () => ({success: !context.failSession, sessions: context.sessions});
vm.createContext(context);
const classSource = `class FreshService {
  async ensureConnected() {return {success:true}}
  getCacheAccountScope() {return 'synthetic-account'}
  accountCacheEpoch = 1
  isAccountCacheContextCurrent() {return !accountChanged}
  getRowField(row, fields) {for(const f of fields) {if(row[f] !== undefined && row[f] !== null && row[f] !== '') return row[f]} return ''}
  ${method}
}; service = new FreshService()`;
vm.runInContext(stripTypeScriptTypes(classSource, {mode:'transform'}), context);
context.rows = [{username:'wxid_a', remark:'first-remark', nick_name:'nick', alias:'alias'}, {username:'one@chatroom', nick_name:'same-name'}, {username:'two@chatroom',nick_name:'same-name'}, {username:'中文昵称',remark:'reject'}];
context.sessions = [{username:'wxid_a'}, {username:'one@chatroom'}, {username:'two@chatroom'}, {username:'wxid_unlisted',displayName:'old-cache-must-not-be-used'}];
let result = await context.service.getFreshIdentitySnapshot();
assert.equal(result.success,true); assert.equal(result.contacts.length,3);
assert.equal(result.contacts.find(x => x.username==='wxid_a').displayName,'first-remark');
assert.equal(result.sessions.find(x => x.username==='wxid_unlisted').displayName,'wxid_unlisted');
assert.equal(result.sessions.filter(x => x.displayName==='same-name').length,2);
context.rows[0].remark=''; context.rows[0].nick_name='renamed'; context.rows[0].alias='';
result = await context.service.getFreshIdentitySnapshot();
assert.equal(result.contacts.find(x => x.username==='wxid_a').displayName,'renamed');
assert.equal(result.contacts.find(x => x.username==='wxid_a').remark,'');
context.accountChanged=true;
assert.equal((await context.service.getFreshIdentitySnapshot()).success,false);
context.accountChanged=false; context.failContact=true;
assert.equal((await context.service.getFreshIdentitySnapshot()).success,false);
context.failContact=false; context.failSession=true;
assert.equal((await context.service.getFreshIdentitySnapshot()).success,false);
context.failSession=false;
// Deadline never releases a still-hung native read. Repeated polls reuse a
// settled failure reply without appending more worker RPCs/reaction closures.
let nativeCalls=0, settleContact;
const originalQuery=context.wcdbService.execQuery;
context.wcdbService.execQuery=()=>{nativeCalls++;return new Promise(resolve=>{settleContact=resolve})};
await new Promise(setImmediate);
const first=context.service.getFreshIdentitySnapshot();
for(let i=0;i<20;i++) assert.equal(context.service.getFreshIdentitySnapshot(),first);
await new Promise(setImmediate);
assert.equal(nativeCalls,1); assert.equal(timers.size,1);
[...timers.values()][0]();
assert.equal((await first).success,false);
for(let i=0;i<20;i++) assert.equal(context.service.getFreshIdentitySnapshot(),first);
assert.equal(nativeCalls,1);
settleContact({success:true,rows:context.rows});
await new Promise(setImmediate);
context.wcdbService.execQuery=originalQuery;
assert.equal((await context.service.getFreshIdentitySnapshot()).success,true);

const http = added('httpService.ts');
const handler = http.slice(http.indexOf('  private async handleIdentitySnapshot('),http.length);
context.chatService=context.service; context.json=null; context.error=null;
vm.runInContext(stripTypeScriptTypes(`class Handler {
  sendJson(res, value) {json=value}
  sendError(res, status, message) {error={status,message}}
  ${handler}
}; handler = new Handler()`,{mode:'transform'}), context);
const response = {headers:{},status:0,ended:false,setHeader(k,v){this.headers[k]=v},writeHead(n){this.status=n},end(){this.ended=true}};
await context.handler.handleIdentitySnapshot({headers:{}},response);
assert.equal(context.json.version,1); assert.equal(context.json.fresh,true); assert.equal(context.json.capabilities.freshIdentitySnapshot,1);
let priorEtag=response.headers.ETag;
context.json=null;
await context.handler.handleIdentitySnapshot({headers:{'if-none-match':priorEtag}},response);
assert.equal(response.status,304); assert.equal(context.json,null);
context.rows[0].remark='new-remark'; response.status=0;
await context.handler.handleIdentitySnapshot({headers:{'if-none-match':priorEtag}},response);
assert.equal(response.status,0); assert.equal(context.json.contacts.find(x => x.username==='wxid_a').displayName,'new-remark');
assert.notEqual(response.headers.ETag,priorEtag);
context.failContact=true;
await context.handler.handleIdentitySnapshot({headers:{}},response);
assert.equal(context.error.status,503);
const routeIndex=http.indexOf("pathname === '/api/v1/identity-snapshot'");
assert.ok(http.slice(routeIndex,routeIndex+300).includes("req.method !== 'GET'"));
console.log('Synthetic identity/rename/snapshot/ETag/error/GET and hung-worker singleflight checks passed; no native calls.');
