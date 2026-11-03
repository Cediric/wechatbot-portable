// Pure mocks: evaluates only our readable factory and one isolated HTTP method.
// It never imports main.js/Electron/native DLLs, reads account data, or opens HTTP.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const workspace=path.resolve(here,'../../..');
const extension=fs.readFileSync(path.join(here,'weflow632_extension.js'),'utf8');
const sandbox={setTimeout,clearTimeout,Buffer};
vm.createContext(sandbox);
vm.runInContext(extension,sandbox,{timeout:1000});
const install=sandbox.__wf632InstallIdentityExtension;
assert.equal(typeof install,'function');
const normalize=value=>JSON.parse(JSON.stringify(value));
// Load only four integrity-checked pure JS XML parser modules from the original
// packaged application. No Electron/main/native modules or account data execute.
function recordedXmlParser(){
  const asar=process.env.WEFLOW_ORIGINAL_ASAR || process.argv[3];
  if (!asar) throw new Error('Set WEFLOW_ORIGINAL_ASAR or pass the original ASAR as argument 3; no machine-specific default is used');
  const fd=fs.openSync(path.resolve(asar),'r');
  try {
    const size=fs.fstatSync(fd).size,prefix=Buffer.alloc(16);
    assert.equal(fs.readSync(fd,prefix,0,16,0),16);
    const total=prefix.readUInt32LE(4),inner=prefix.readUInt32LE(8),length=prefix.readUInt32LE(12);
    assert(prefix.readUInt32LE(0)===4 && total===inner+4 && length>0 && length<=inner-4 && inner<64*1024*1024 && 8+total<size);
    const raw=Buffer.alloc(length);assert.equal(fs.readSync(fd,raw,0,length,16),length);
    const base=8+total,header=JSON.parse(raw.toString('utf8')),entries=new Map();
    const walk=(files,parent='')=>{for(const [key,value] of Object.entries(files)){const name=parent+key;if(value.files)walk(value.files,name+'/');else entries.set(name,value)}};
    walk(header.files);
    const read=(file,maximum)=>{
      const entry=entries.get(file);assert(entry && !entry.unpacked && Number.isSafeInteger(entry.size) && entry.size>0 && entry.size<=maximum);
      const offset=Number(entry.offset);assert(Number.isSafeInteger(offset) && offset>=0 && base+offset+entry.size<=size);
      const source=Buffer.alloc(entry.size);assert.equal(fs.readSync(fd,source,0,source.length,base+offset),source.length);
      assert.equal(entry.integrity?.algorithm,'SHA256');assert.equal(crypto.createHash('sha256').update(source).digest('hex'),entry.integrity.hash);
      return source.toString('utf8');
    };
    const main=read('dist-electron/main.js',4*1024*1024),worker=read('dist-electron/wcdbWorker.js',1024*1024);
    assert.equal(crypto.createHash('sha256').update(main).digest('hex'),'4d504f70c1c29041b3730df30a7aeac20fc213ce53d8d2585eb862c2f8ec36ec');
    const allowed=new Map([['saxes','node_modules/saxes/saxes.js'],
      ['xmlchars/xml/1.0/ed5','node_modules/xmlchars/xml/1.0/ed5.js'],
      ['xmlchars/xml/1.1/ed2','node_modules/xmlchars/xml/1.1/ed2.js'],
      ['xmlchars/xmlns/1.0/ed3','node_modules/xmlchars/xmlns/1.0/ed3.js']]),cache=new Map();
    const load=name=>{
      if(cache.has(name))return cache.get(name);
      const file=allowed.get(name);assert(file,'Unexpected XML parser dependency');
      const module={exports:{}};cache.set(name,module.exports);
      vm.runInNewContext(read(file,256*1024),{exports:module.exports,module,require:load},{timeout:1000,filename:file});
      cache.set(name,module.exports);return module.exports;
    };
    return {SaxesParser:load('saxes').SaxesParser,main,worker};
  } finally {fs.closeSync(fd)}
}
const recorded=recordedXmlParser(),SaxesParser=recorded.SaxesParser;assert.equal(typeof SaxesParser,'function');
let checks=0;
async function test(name,callback){await callback();checks++;console.log('PASS '+name);}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(options={}){
  const calls={connect:0,sql:[],sessions:0,payload:0,sns:[],tables:[],schemas:[]};
  let rows=options.rows||[{username:'wxid_a',remark:'新备注',nick_name:'昵称',alias:'custom_id'},
    {username:'a@chatroom',nick_name:'新群名'},{username:'b@chatroom',nick_name:'新群名'}];
  let sessions=options.sessions||[{username:'wxid_a',displayName:'旧备注'},
    {talkerId:'a@chatroom',displayName:'旧群名'},{UsrName:'b@chatroom',displayName:'旧群名'}];
  let timeline=options.timeline||[{id:'9007199254740993123',username:'wxid_a',createTime:1700000000,
    rawXml:'<id>9007199254740993123</id><username>wxid_a</username>',
    comments:[{id:'71',username:'wxid_member',content:'synthetic comment',refCommentId:'70',refUsername:'wxid_a',nativeExtra:1}],
    likes:['synthetic display name'],nativeExtra:{retained:true}}];
  let detailsRows=options.detailsRows||[{tid:'-1',user_name:'wxid_a',content:'<TimelineObject><id>18446744073709551615</id><username>wxid_a</username><createTime>1700000000</createTime><contentDesc>synthetic post</contentDesc></TimelineObject><LocalExtraInfo><like_user_list><user_comment><b_deleted>0</b_deleted><is_local_added>0</is_local_added><username>wxid_a</username><nickname>synthetic name</nickname></user_comment></like_user_list><comment_user_list><user_comment><b_deleted>0</b_deleted><is_local_added>0</is_local_added><id>71</id><username>wxid_member</username><nickname>synthetic name</nickname><content>synthetic comment</content><ref_comment_id>70</ref_comment_id><ref_username>wxid_a</ref_username><create_time>1700000010</create_time></user_comment></comment_user_list></LocalExtraInfo>'}];
  const chat={accountCacheEpoch:1,scope:'synthetic-account',
    async ensureConnected(){calls.connect++;return {success:true}},
    getCacheAccountScope(){return this.scope},
    isAccountCacheContextCurrent(scope,epoch){return !!scope&&this.scope===scope&&this.accountCacheEpoch===epoch},
    configService:{get(key){return key==='dbPath'?'C:\\synthetic-db':key==='myWxid'?'wxid_a':''},getAccountDir(){return 'C:\\synthetic-db\\wxid_a'}}};
  const wcdb={async execQuery(...args){calls.sql.push(args);return {success:true,rows:args[0]==='sns'?detailsRows:rows}},
    async getSessions(){calls.sessions++;return {success:true,sessions}},
    async getSnsTimeline(...args){calls.sns.push(args);return {success:true,timeline}},
    async listTables(...args){calls.tables.push(args);return {success:true,tables:['SnsTimeLine','OtherMetadata']}},
    async getTableSchema(...args){calls.schemas.push(args);return {success:true,schema:'CREATE TABLE SnsTimeLine (tid INTEGER PRIMARY KEY, userName TEXT, content BLOB, createTime INTEGER)'}}};
  const http={sendJson(res,body){res.body=body;res.status=200},
    sendError(res,status,error){res.status=status;res.body={success:false,error}},
    sendMethodNotAllowed(res,allowed){res.status=405;res.headers.Allow=allowed},
    toSnsMediaKey(value){if(value==null)return;if(typeof value==='number'&&Number.isFinite(value))return value;const text=String(value).trim();if(text)return /^-?\d+$/.test(text)?Number(text):text}};
  const push={marker:'original-this',async buildPayload(session,message){
    assert.equal(this.marker,'original-this');calls.payload++;
    return {event:message.event||'message.new',content:message.content||'',sessionId:session.username,
      rawid:message.rawid||'server-1',originalField:'retained'};}};
  const fixedFs={realpathSync(value){return path.win32.resolve(value)},statSync(){return {isFile(){return true}}}};
  install({chatService:chat,wcdbService:wcdb,httpService:http,messagePushService:push,crypto,fs:fixedFs,path:path.win32,SaxesParser,...options.deps});
  return {chat,wcdb,http,push,calls,fixedFs,setRows(value){rows=value},setSessions(value){sessions=value},setTimeline(value){timeline=value},setDetailsRows(value){detailsRows=value}};
}
function response(){return {headers:{},setHeader(name,value){this.headers[name]=value},
  writeHead(status){this.status=status},end(body){this.ended=true;if(body!==undefined)this.body=JSON.parse(body)}};}
async function request(f,headers={}){const res=response();await f.http.handleFreshIdentitySnapshot632({method:'GET',headers},res);return res;}
async function snsRequest(f,query='',method='GET'){
  const res=response();await f.http.handleSnsIdentityTimeline632({method,headers:{}},new URL('http://127.0.0.1/api/v1/sns-identity-timeline'+query),res);return res;
}
async function schemaRequest(f,query='',method='GET'){
  const res=response();await f.http.handleSnsIdentitySchema632({method,headers:{}},new URL('http://127.0.0.1/api/v1/sns-identity-schema'+query),res);return res;
}
async function detailsRequest(f,query='?post_ids=18446744073709551615',method='GET'){
  const res=response();await f.http.handleSnsIdentityDetails632({method,headers:{}},new URL('http://127.0.0.1/api/v1/sns-identity-details'+query),res);return res;
}

await test('install is inert; no DB/connection until endpoint requested',async()=>{
  const f=fixture();assert.equal(f.calls.connect,0);assert.equal(f.calls.sql.length,0);assert.equal(f.calls.sessions,0);
});
await test('SNS proxy decimal uint64 keys remain exact strings without numeric rounding',async()=>{
  const f=fixture(),key='18446744073709551615';
  assert.equal(f.http.toSnsMediaKey(key),key);
  assert.equal(f.http.toSnsMediaKey('  '+key+'  '),key);
  assert.equal(f.http.toSnsMediaKey(12),12);
  assert.equal(f.http.toSnsMediaKey(undefined),undefined);
  assert.equal((await request(f)).body.capabilities.snsMediaKey,1);
});
await test('fresh fixed read-only SQL, same-name groups remain separate and session stale names ignored',async()=>{
  const f=fixture(),res=await request(f);
  assert.equal(res.status,200);assert.equal(res.body.version,1);assert.equal(res.body.fresh,true);
  assert.deepEqual(normalize(f.calls.sql),[['contact',null,'SELECT * FROM contact']]);
  assert.equal(f.calls.sessions,1);assert.equal(res.headers['Cache-Control'],'no-store');
  assert.deepEqual(normalize(res.body.sessions),[
    {username:'a@chatroom',displayName:'新群名',type:2},
    {username:'b@chatroom',displayName:'新群名',type:2},
    {username:'wxid_a',displayName:'新备注',type:1}]);
});
await test('remark removal and every installed raw-session identity alias',async()=>{
  const aliases=['username','user_name','userName','usrName','UsrName','talker','talker_id','talkerId'];
  const f=fixture({rows:[{USERNAME:'wxid_a',WCDB_CT_remark:'',WCDB_CT_nick_name:'现在昵称'}],
    sessions:aliases.map((key,index)=>({[key]:'wxid_'+index}))});
  const res=await request(f);assert.equal(res.body.contacts[0].remark,'');assert.equal(res.body.contacts[0].displayName,'现在昵称');
  assert.deepEqual(normalize(res.body.sessions.map(row=>row.username)),aliases.map((_,index)=>'wxid_'+index));
  assert.equal(res.body.sessions[0].displayName,'wxid_0');
});
await test('unknown display names cannot create an identity; duplicate authoritative identities fail',async()=>{
  const f=fixture({rows:[{username:'同名',nick_name:'昵称'},{nickname:'名字'},{username:'@chatroom'},{username:'unknown'}],
    sessions:[{displayName:'旧名称'},{username:'null'}]});
  let res=await request(f);assert.equal(res.status,200);assert.equal(res.body.contacts.length,0);assert.equal(res.body.sessions.length,0);
  f.setRows([{username:'wxid_a'},{username:'wxid_a'}]);res=await request(f);assert.equal(res.status,503);
  f.setRows([]);f.setSessions([{username:'a@chatroom'},{talker:'a@chatroom'}]);res=await request(f);assert.equal(res.status,503);
});
await test('ETag 304 still performs a fresh database read; rename changes revision',async()=>{
  const f=fixture(),first=await request(f);const second=await request(f,{'if-none-match':first.headers.ETag});
  assert.equal(second.status,304);assert.equal(second.body,undefined);assert.equal(f.calls.sql.length,2);
  f.setRows([{username:'wxid_a',remark:'再次改名'}]);const third=await request(f,{'if-none-match':first.headers.ETag});
  assert.equal(third.status,200);assert.notEqual(third.headers.ETag,first.headers.ETag);assert.equal(f.calls.sql.length,3);
});
await test('account switch during contacts or raw sessions cannot return a mixed snapshot',async()=>{
  const f=fixture();f.wcdb.execQuery=async()=>{f.chat.accountCacheEpoch++;return {success:true,rows:[]}};
  let res=await request(f);assert.equal(res.status,503);assert.equal(f.calls.sessions,0);
  f.wcdb.execQuery=async()=>({success:true,rows:[]});f.wcdb.getSessions=async()=>{f.chat.scope='another-synthetic-account';return {success:true,sessions:[]}};
  res=await request(f);assert.equal(res.status,503);
});
await test('native failure/invalid result returns 503 without cached-name fallback',async()=>{
  const f=fixture();f.wcdb.execQuery=async()=>{throw new Error('synthetic native failure')};
  assert.equal((await request(f)).status,503);assert.equal(f.calls.sessions,0);
  f.wcdb.execQuery=async()=>({success:true,rows:null});assert.equal((await request(f)).status,503);
});
await test('shared deadline bounds HTTP reply while hung native query gets one RPC for repeated polls',async()=>{
  let timeout,release,readCount=0;
  const f=fixture({deps:{setTimeout(callback,ms){assert.equal(ms,2500);timeout=callback;return 1},clearTimeout(){}}});
  f.wcdb.execQuery=()=>{readCount++;return new Promise(resolve=>{release=resolve})};
  const first=f.chat.getFreshIdentitySnapshot();await tick();
  for(let i=0;i<40;i++)assert.equal(f.chat.getFreshIdentitySnapshot(),first);
  timeout();assert.equal((await first).success,false);
  for(let i=0;i<40;i++)assert.equal(f.chat.getFreshIdentitySnapshot(),first);
  assert.equal(readCount,1);assert.equal(f.calls.connect,1);
  assert.equal((await request(f)).status,503);
  release({success:true,rows:[]});await tick();
  f.wcdb.execQuery=async()=>{readCount++;return {success:true,rows:[]}};
  const next=f.chat.getFreshIdentitySnapshot();assert.notEqual(next,first);assert.equal((await next).success,true);assert.equal(readCount,2);
});
await test('deadline also coalesces hung ensureConnected before any native read',async()=>{
  let timeout,release,connections=0;
  const f=fixture({deps:{setTimeout(callback){timeout=callback;return 1},clearTimeout(){}}});
  f.chat.ensureConnected=()=>{connections++;return new Promise(resolve=>{release=resolve})};
  const pending=f.chat.getFreshIdentitySnapshot();timeout();assert.equal((await pending).success,false);
  for(let i=0;i<40;i++)await f.chat.getFreshIdentitySnapshot();assert.equal(connections,1);assert.equal(f.calls.sql.length,0);
  release({success:true});await tick();
});
await test('SSE retains all original fields and carries real sender/direction/native type/local ID',async()=>{
  const f=fixture(),message={senderUsername:'wxid_member',isSend:0,localType:10000,localId:73,content:'synthetic system event'};
  const payload=await f.push.buildPayload({username:'a@chatroom'},message);
  assert.equal(payload.originalField,'retained');assert.equal(payload.senderUsername,'wxid_member');assert.equal(payload.isSend,0);
  assert.equal(payload.localType,10000);assert.equal(payload.localId,73);assert.equal(payload.sessionId,'a@chatroom');
  const revoke=await f.push.buildPayload({username:'a@chatroom'},{...message,event:'message.revoke'});
  assert.equal(revoke.event,'message.revoke');assert.equal(revoke.senderUsername,undefined);
});
await test('POST rejected before fresh DB access; duplicate/missing dependency installation refused',async()=>{
  const f=fixture(),res=response();await f.http.handleFreshIdentitySnapshot632({method:'POST',headers:{}},res);
  assert.equal(res.status,405);assert.equal(f.calls.connect,0);
  assert.throws(()=>install({chatService:f.chat,wcdbService:f.wcdb,httpService:f.http,messagePushService:f.push,crypto}),/already installed/);
  assert.throws(()=>install({}),/missing ensureConnected/);
});

await test('SNS native passthrough retains actual actor/ref/id/fragment and calls no SQL or export',async()=>{
  const f=fixture(),res=await snsRequest(f,'?limit=100&offset=200&usernames=wxid_a,wxid_b,wxid_a');
  assert.equal(res.status,200);assert.equal(res.body.success,true);assert.equal(res.body.version,1);
  assert.equal(res.body.capabilities.snsIdentityTimeline,1);assert.equal(res.headers['Cache-Control'],'no-store');
  assert.deepEqual(normalize(f.calls.sns),[[100,200,['wxid_a','wxid_b'],null,null,null]]);
  const row=res.body.timeline[0];assert.equal(row.id,'9007199254740993123');assert.equal(row.comments[0].username,'wxid_member');
  assert.equal(row.comments[0].refUsername,'wxid_a');assert.equal(row.comments[0].id,'71');assert.equal(row.comments[0].nativeExtra,1);
  assert.equal(row.rawXml,'<id>9007199254740993123</id><username>wxid_a</username>');assert.equal(row.nativeExtra.retained,true);
  assert.equal(f.calls.sql.length,0);assert.equal(f.calls.sessions,0);
});
await test('SNS defaults mirror native filter contract and never invent missing actors',async()=>{
  const f=fixture({timeline:[{id:'31',username:'wxid_a',comments:[{id:'native-local-id',nickname:'same name',content:'context only'}]}]});
  const res=await snsRequest(f);assert.equal(res.status,200);assert.deepEqual(normalize(f.calls.sns),[[20,0,null,null,null,null]]);
  assert.equal(res.body.timeline[0].comments[0].username,undefined);assert.equal(res.body.timeline[0].comments[0].id,'native-local-id');
});
await test('SNS rejects unsupported SQL/path, malformed/duplicate/boundary parameters before any native access',async()=>{
  const f=fixture();
  for(const query of ['?limit=0','?limit=101','?offset=201','?offset=-1','?limit=1.5','?limit=true','?offset=NaN',
    '?limit=20&limit=30','?usernames=not%20an%20id','?sql=SELECT','?path=C%3A%5Cprivate','?force=1',
    '?usernames='+Array.from({length:101},(_,i)=>'wxid_'+i).join(',')]) {
    assert.equal((await snsRequest(f,query)).status,400,query);
  }
  assert.equal((await snsRequest(f,'','POST')).status,405);assert.equal(f.calls.connect,0);assert.equal(f.calls.sns.length,0);
});
await test('SNS account switch, native failures and invalid/oversized rows fail without cached fallback',async()=>{
  const f=fixture();f.wcdb.getSnsTimeline=async()=>{f.chat.accountCacheEpoch++;return {success:true,timeline:[]}};
  assert.equal((await snsRequest(f)).status,503);
  for(const value of [{success:false,timeline:[]},{success:true,timeline:null},{success:true,timeline:[null]},
    {success:true,timeline:[{} , {}]},{success:true,timeline:[{content:'x'.repeat(8*1024*1024)}]}]) {
    f.wcdb.getSnsTimeline=async()=>value;
    assert.equal((await snsRequest(f,'?limit=1')).status,503);
  }
  f.wcdb.getSnsTimeline=async()=>{throw new Error('synthetic failure')};assert.equal((await snsRequest(f)).status,503);
});
await test('SNS singleflight shares identical params; different offsets cannot enqueue while native hangs after deadline',async()=>{
  let timeout,release,reads=0;
  const f=fixture({deps:{setTimeout(callback,ms){assert.equal(ms,2500);timeout=callback;return 1},clearTimeout(){}}});
  f.wcdb.getSnsTimeline=()=>{reads++;return new Promise(resolve=>{release=resolve})};
  const parameters={limit:20,offset:0,usernames:[]};
  const first=f.chat.getSnsIdentityTimeline632(parameters);await tick();
  for(let i=0;i<40;i++)assert.equal(f.chat.getSnsIdentityTimeline632(parameters),first);
  timeout();assert.equal((await first).success,false);
  for(let i=0;i<40;i++)assert.equal(f.chat.getSnsIdentityTimeline632(parameters),first);
  assert.equal((await f.chat.getSnsIdentityTimeline632({...parameters,offset:1})).success,false);
  assert.equal(reads,1);assert.equal(f.calls.connect,1);
  release({success:true,timeline:[]});await tick();
  f.wcdb.getSnsTimeline=async()=>{reads++;return {success:true,timeline:[]}};
  const next=f.chat.getSnsIdentityTimeline632(parameters);assert.notEqual(next,first);assert.equal((await next).success,true);assert.equal(reads,2);
});
await test('SNS hung connection stays singleflight and account changes cannot share an old snapshot',async()=>{
  let timeout,release,connections=0;
  const f=fixture({deps:{setTimeout(callback){timeout=callback;return 1},clearTimeout(){}}});
  f.chat.ensureConnected=()=>{connections++;return new Promise(resolve=>{release=resolve})};
  const params={limit:20,offset:0,usernames:[]},pending=f.chat.getSnsIdentityTimeline632(params);
  timeout();assert.equal((await pending).success,false);
  f.chat.accountCacheEpoch++;
  for(let i=0;i<40;i++)assert.equal((await f.chat.getSnsIdentityTimeline632(params)).success,false);
  assert.equal(connections,1);assert.equal(f.calls.sns.length,0);
  release({success:true});await tick();
});

await test('SNS schema exposes only table names and whitelisted column names/types, never SQL or defaults',async()=>{
  const f=fixture();
  f.wcdb.getTableSchema=async(...args)=>{f.calls.schemas.push(args);return {success:true,schema:`CREATE TABLE "SnsTimeLine" (
    "tid" INTEGER PRIMARY KEY, [userName] TEXT DEFAULT 'secret,''value,with,commas',
    content BLOB NOT NULL, createTime INTEGER, amount DECIMAL(10,2),
    CONSTRAINT check_content CHECK(length(content)>0), UNIQUE(tid,userName)) WITHOUT ROWID;`}};
  const res=await schemaRequest(f,'?access_token=synthetic');
  assert.equal(res.status,200);assert.equal(res.body.version,1);assert.equal(res.body.capabilities.snsIdentitySchema,1);
  assert.deepEqual(normalize(res.body.tableNames),['OtherMetadata','SnsTimeLine']);
  assert.deepEqual(normalize(f.calls.tables),[['sns','']]);
  assert.deepEqual(normalize(f.calls.schemas),[['sns','','SnsTimeLine']]);
  assert.deepEqual(normalize(res.body.tables),[{name:'SnsTimeLine',columns:[
    {name:'tid',type:'INTEGER'},{name:'userName',type:'TEXT'},{name:'content',type:'BLOB'},
    {name:'createTime',type:'INTEGER'},{name:'amount',type:'DECIMAL(10,2)'}]}]);
  assert.equal(res.headers['Cache-Control'],'no-store');
  assert(!JSON.stringify(res.body).includes('secret'));assert(!JSON.stringify(res.body).includes('CREATE'));
  assert.equal(f.calls.sql.length,0);assert.equal(f.calls.sns.length,0);
});
await test('SNS schema rejects all client SQL/path/table/filter parameters and POST before connection',async()=>{
  const f=fixture();
  for(const query of ['?sql=SELECT+1','?path=some.db','?dbPath=some.db','?table=SnsTimeLine','?limit=1','?offset=0','?usernames=wxid_a','?access_token=a&access_token=b']) {
    assert.equal((await schemaRequest(f,query)).status,400);
  }
  assert.equal((await schemaRequest(f,'','POST')).status,405);
  assert.equal(f.calls.connect,0);assert.equal(f.calls.tables.length,0);assert.equal(f.calls.schemas.length,0);
});
await test('SNS schema native failures, invalid table names and malformed DDL fail closed without native errors',async()=>{
  const f=fixture();
  for(const result of [{success:false,error:'private/path.db'},{success:true,tables:['bad;name']},
      {success:true,tables:['SnsTimeLine','SnsTimeLine']},{success:true,tables:Array(129).fill('OtherMetadata')},
      {success:true,tables:null}]) {
    f.wcdb.listTables=async()=>result;
    const res=await schemaRequest(f);assert.equal(res.status,503);assert(!JSON.stringify(res.body).includes('private'));
  }
  f.wcdb.listTables=async()=>({success:true,tables:['SnsTimeLine']});
  for(const schema of ['CREATE TABLE AnotherTable (tid INTEGER)','CREATE TABLE SnsTimeLine (a TEXT); SELECT 1',
      'CREATE TABLE SnsTimeLine (a TEXT, a INTEGER)','CREATE TABLE SnsTimeLine (a UNKNOWNTYPE)',
      "CREATE TABLE SnsTimeLine (a TEXT DEFAULT 'private_value)",'x'.repeat(65537)]) {
    f.wcdb.getTableSchema=async()=>({success:true,schema});
    const res=await schemaRequest(f);assert.equal(res.status,503);assert(!JSON.stringify(res.body).includes('private_value'));
  }
  f.wcdb.getTableSchema=async()=>{throw Error('private_db_key/path')};
  const res=await schemaRequest(f);assert.equal(res.status,503);assert(!JSON.stringify(res.body).includes('private_db_key'));
});
await test('SNS schema verifies account scope/epoch after every native read',async()=>{
  const f=fixture();
  f.wcdb.listTables=async()=>{f.chat.accountCacheEpoch++;return {success:true,tables:['SnsTimeLine']}};
  assert.equal((await schemaRequest(f)).status,503);assert.equal(f.calls.schemas.length,0);
  f.wcdb.listTables=async()=>({success:true,tables:['SnsTimeLine']});
  f.wcdb.getTableSchema=async()=>{f.chat.scope='different-synthetic-account';return {success:true,schema:'CREATE TABLE SnsTimeLine (tid INTEGER)'}};
  assert.equal((await schemaRequest(f)).status,503);
});
await test('SNS schema and timeline share one bounded native flight even after deadline and account change',async()=>{
  let timeout,release,reads=0;
  const f=fixture({deps:{setTimeout(callback,ms){assert.equal(ms,2500);timeout=callback;return 1},clearTimeout(){}}});
  f.wcdb.listTables=()=>{reads++;return new Promise(resolve=>{release=resolve})};
  const first=f.chat.getSnsIdentitySchema632();await tick();
  for(let i=0;i<20;i++)assert.equal(f.chat.getSnsIdentitySchema632(),first);
  assert.equal((await snsRequest(f)).status,503);assert.equal(f.calls.sns.length,0);
  timeout();assert.equal((await first).success,false);
  for(let i=0;i<20;i++)assert.equal(f.chat.getSnsIdentitySchema632(),first);
  f.chat.accountCacheEpoch++;assert.equal((await schemaRequest(f)).status,503);assert.equal(reads,1);
  release({success:true,tables:['SnsTimeLine']});await tick();assert.equal(f.calls.schemas.length,0);
  f.wcdb.listTables=async()=>({success:true,tables:['OtherMetadata']});
  const next=f.chat.getSnsIdentitySchema632();assert.notEqual(next,first);assert.equal((await next).success,true);
});
await test('SNS schema cannot run alongside a hung timeline, and absent whitelist tables never trigger arbitrary schema reads',async()=>{
  let release;
  const f=fixture();f.wcdb.getSnsTimeline=()=>new Promise(resolve=>{release=resolve});
  const timeline=f.chat.getSnsIdentityTimeline632({limit:20,offset:0,usernames:[]});await tick();
  assert.equal((await schemaRequest(f)).status,503);assert.equal(f.calls.tables.length,0);
  release({success:true,timeline:[]});await timeline;await tick();
  f.wcdb.listTables=async()=>({success:true,tables:['SnsInfo','SnsComment','OtherMetadata']});
  const res=await schemaRequest(f);assert.equal(res.status,200);assert.deepEqual(normalize(res.body.tables),[]);
  assert.equal(f.calls.schemas.length,0);
});
await test('SNS generic empty metadata uses only the selected account fixed SNS filename',async()=>{
  const f=fixture();
  f.wcdb.listTables=async(...args)=>{f.calls.tables.push(args);return {success:true,tables:args[1]?['SnsTimeLine']:[]}};
  const res=await schemaRequest(f);assert.equal(res.status,200);
  assert.deepEqual(normalize(f.calls.tables),[['sns',''],['sns','C:\\synthetic-db\\wxid_a\\db_storage\\sns\\sns.db']]);
  assert.deepEqual(normalize(f.calls.schemas),[['sns','C:\\synthetic-db\\wxid_a\\db_storage\\sns\\sns.db','SnsTimeLine']]);
  assert(!JSON.stringify(res.body).includes('synthetic-db'));
});
await test('SNS internal fixed path fails closed for junction escape, absent file and account switch',async()=>{
  for(const condition of ['escape','missing','account']) {
    const f=fixture();f.wcdb.listTables=async(...args)=>{f.calls.tables.push(args);return {success:true,tables:[]}};
    if(condition==='escape')f.fixedFs.realpathSync=value=>value.endsWith('sns.db')?'D:\\outside\\sns.db':path.win32.resolve(value);
    if(condition==='missing')f.fixedFs.statSync=()=>{throw Error('private_account_path')};
    if(condition==='account')f.fixedFs.realpathSync=value=>{f.chat.accountCacheEpoch++;return path.win32.resolve(value)};
    const res=await schemaRequest(f);assert.equal(res.status,503);assert.equal(f.calls.tables.length,1);assert.equal(f.calls.schemas.length,0);
    assert(!JSON.stringify(res.body).includes('private_account_path'));assert(!JSON.stringify(res.body).includes('outside'));
  }
});

await test('SNS details maps unsigned post ID to exact signed DB tid and verified XML comment actors',async()=>{
  const f=fixture(),res=await detailsRequest(f);assert.equal(res.status,200);assert.equal(res.body.version,1);assert.equal(res.body.capabilities.snsIdentityDetails,1);
  const row=res.body.timeline[0];assert.equal(row.id,'18446744073709551615');assert.equal(row.tid,'-1');assert.equal(row.username,'wxid_a');
  assert.equal(row.contentDesc,'synthetic post');assert.equal(row.createTime,1700000000);assert.equal(row.identityVerified,true);
  assert.equal(row.commentsVerified,true);assert.equal(row.likesVerified,true);
  assert.deepEqual(normalize(row.commentsDetail),[{id:'71',username:'wxid_member',nickname:'synthetic name',content:'synthetic comment',refCommentId:'70',refUsername:'wxid_a',refNickname:'',createTime:1700000010,commentId:'71',comment64Id:'',refCommentId32:'70',refCommentId64:''}]);
  assert.deepEqual(normalize(row.likesDetail),[{username:'wxid_a',nickname:'synthetic name'}]);
  assert.deepEqual(normalize(f.calls.sql),[['sns','C:\\synthetic-db\\wxid_a\\db_storage\\sns\\sns.db',"SELECT CAST(tid AS TEXT) AS tid, user_name, content FROM SnsTimeLine WHERE tid IN ('-1') LIMIT 20"]]);
  assert.equal(f.calls.sns.length,0);assert.equal(f.calls.schemas.length,0);assert.equal(f.calls.tables.length,0);
  assert(!JSON.stringify(res.body).includes('TimelineObject>'));assert(!JSON.stringify(res.body).includes('synthetic-db'));
});
await test('SNS details only accepts canonical unsigned64 post_ids, max20 and GET; no caller SQL/path/filter',async()=>{
  const f=fixture();
  for(const query of ['', '?post_ids=0','?post_ids=-1','?post_ids=01','?post_ids=18446744073709551616',
      '?post_ids=1,1','?post_ids=1&post_ids=2','?post_ids=1&sql=SELECT+1','?post_ids=1&path=some.db',
      '?post_ids=1&limit=20','?post_ids=1&offset=0','?post_ids=1&usernames=wxid_a',
      '?post_ids='+Array.from({length:21},(_,i)=>i+1).join(',')])assert.equal((await detailsRequest(f,query)).status,400);
  assert.equal((await detailsRequest(f,undefined,'POST')).status,405);assert.equal(f.calls.connect,0);assert.equal(f.calls.sql.length,0);
});
await test('SNS details accepts the observed single SnsDataItem wrapper and rejects mixed XML sources',async()=>{
  const f=fixture(),inner='<TimelineObject><id>18446744073709551615</id><username>wxid_a</username><createTime>1700000000</createTime><contentDesc>x</contentDesc></TimelineObject><LocalExtraInfo><like_user_list/><comment_user_list/></LocalExtraInfo>';
  f.setDetailsRows([{tid:'-1',user_name:'wxid_a',content:'<SnsDataItem>'+inner+'</SnsDataItem>'}]);
  let res=await detailsRequest(f);assert.equal(res.status,200);assert.equal(res.body.timeline[0].identityVerified,true);assert.equal(res.body.timeline[0].commentsVerified,true);
  f.setDetailsRows([{tid:'-1',user_name:'wxid_a',content:'<SnsDataItem>'+inner+'</SnsDataItem><LocalExtraInfo/>'}]);
  res=await detailsRequest(f);assert.equal(res.status,503);assert.equal(res.body.success,false);
});
await test('SNS details returns real 503 failures with the exact original two-argument sendJson',async()=>{
  const marker='sendJson(e,t){e.setHeader(`Content-Type`,`application/json; charset=utf-8`)',start=recorded.main.indexOf(marker),end=recorded.main.indexOf('sendMethodNotAllowed(',start);
  assert(start>=0&&end>start&&end-start<300);
  const realJson=vm.runInNewContext('({'+recorded.main.slice(start,end)+'}).sendJson',{}, {timeout:1000});
  const f=fixture();f.http.sendJson=realJson;
  let res=await detailsRequest(f);assert.equal(res.status,200);assert.equal(res.body.success,true);
  f.setDetailsRows([]);res=await detailsRequest(f);assert.equal(res.status,503);assert.equal(res.body.success,false);assert.equal(res.body.diagnostics.stage,'query');
});
await test('SNS details rejects mismatched ID/tid/DB author/XML author, malformed/DTD XML and missing rows',async()=>{
  const f=fixture(),content=f=>'<TimelineObject><id>18446744073709551615</id><username>'+f+'</username><createTime>1700000000</createTime><contentDesc>x</contentDesc></TimelineObject>';
  for(const rows of [[],[{tid:'-2',user_name:'wxid_a',content:content('wxid_a')}],
      [{tid:'-1',user_name:'wxid_a',content:content('wxid_b')}],
      [{tid:'-1',user_name:'wxid_a',content:content('wxid_a').replace('18446744073709551615','18446744073709551614')}],
      [{tid:'-1',user_name:'wxid_a',content:'<!DOCTYPE x [<!ENTITY x "private">]>'+content('wxid_a')}],
      [{tid:'-1',user_name:'wxid_a',content:'<?nonstandard private?>'+content('wxid_a')}],
      [{tid:'-1',user_name:'wxid_a',content:content('wxid_a')+'<broken>'}],
      [{tid:'-1',user_name:'wxid_a',content:content('wxid_a').replace('</TimelineObject>','<LocalExtraInfo/></TimelineObject>')+'<LocalExtraInfo/>'}],
      [{tid:'-1',user_name:'wxid_a',content:'x'.repeat(8*1024*1024+1)}]]) {
    f.setDetailsRows(rows);const res=await detailsRequest(f);assert.equal(res.status,503);assert(!JSON.stringify(res.body).includes('private'));
  }
});
await test('SNS details missing actor, missing real comment id, duplicate IDs or absent lists never elevate cached identities',async()=>{
  const f=fixture();
  const header='<TimelineObject><id>18446744073709551615</id><username>wxid_a</username><createTime>1700000000</createTime><contentDesc>x</contentDesc></TimelineObject>';
  for(const xml of [header,header+'<LocalExtraInfo><comment_user_list><user_comment><b_deleted>0</b_deleted><is_local_added>0</is_local_added><id>71</id><nickname>same name</nickname><content>x</content></user_comment></comment_user_list></LocalExtraInfo>',
      header+'<LocalExtraInfo><comment_user_list><user_comment><b_deleted>0</b_deleted><is_local_added>0</is_local_added><id>cmt_123_0.1</id><username>wxid_b</username></user_comment></comment_user_list></LocalExtraInfo>',
      header+'<LocalExtraInfo><comment_user_list>'+Array(2).fill('<user_comment><b_deleted>0</b_deleted><is_local_added>0</is_local_added><id>71</id><username>wxid_b</username></user_comment>').join('')+'</comment_user_list></LocalExtraInfo>']) {
    f.setDetailsRows([{tid:'-1',user_name:'wxid_a',content:xml}]);const res=await detailsRequest(f);assert.equal(res.status,200);
    assert.equal(res.body.timeline[0].commentsVerified,false);assert.equal(res.body.timeline[0].likesVerified,false);
    assert.deepEqual(normalize(res.body.timeline[0].commentsDetail),[]);
  }
  f.setDetailsRows([{tid:'-1',user_name:'wxid_a',content:header+'<LocalExtraInfo><comment_user_list/><like_user_list/></LocalExtraInfo>'}]);
  const res=await detailsRequest(f);assert.equal(res.body.timeline[0].commentsVerified,true);assert.equal(res.body.timeline[0].likesVerified,true);
});
const syntheticDetailsHeader='<TimelineObject><id>18446744073709551615</id><username>wxid_a</username><createTime>1700000000</createTime><contentDesc>synthetic post</contentDesc></TimelineObject>';
const syntheticComment=(id,body='',flags='<b_deleted>0</b_deleted><is_local_added>0</is_local_added>')=>'<user_comment>'+flags+'<comment_id>'+id+'</comment_id><username>wxid_member</username><content>synthetic comment</content>'+body+'</user_comment>';
async function detailsInteractions(comments,likes='') {
  const f=fixture({detailsRows:[{tid:'-1',user_name:'wxid_a',content:syntheticDetailsHeader+'<LocalExtraInfo><comment_user_list>'+comments+'</comment_user_list><like_user_list>'+likes+'</like_user_list></LocalExtraInfo>'}]});
  const res=await detailsRequest(f);assert.equal(res.status,200);return res.body.timeline[0];
}
await test('SNS details preserves exact unsigned64 comment IDs and resolves 32-bit reply aliases',async()=>{
  const row=await detailsInteractions(syntheticComment('71','<comment_64id>18446744073709551615</comment_64id>')+
    syntheticComment('72','<comment_64id>9007199254740993123</comment_64id><ref_comment_id>71</ref_comment_id><ref_comment_64id>0</ref_comment_64id><ref_username>wxid_member</ref_username>'));
  assert.equal(row.commentsVerified,true);assert.equal(row.commentsDetail[0].id,'18446744073709551615');
  assert.equal(row.commentsDetail[1].id,'9007199254740993123');assert.equal(row.commentsDetail[1].refCommentId,'18446744073709551615');
  assert.equal(row.commentsDetail[1].refCommentId32,'71');assert.equal(row.commentsDetail[1].refCommentId64,'');assert.equal(row.diagnostics.commentsWith64Id,2);
});
await test('SNS details excludes deleted comments and likes without using their stale identities',async()=>{
  const row=await detailsInteractions(syntheticComment('71')+'<user_comment><b_deleted>1</b_deleted></user_comment>',
    '<user_comment><b_deleted>0</b_deleted><is_local_added>0</is_local_added><username>wxid_a</username></user_comment><user_comment><b_deleted>1</b_deleted></user_comment>');
  assert.equal(row.commentsVerified,true);assert.equal(row.likesVerified,true);assert.equal(row.commentsDetail.length,1);assert.equal(row.likesDetail.length,1);
  assert.equal(row.diagnostics.deletedCommentElements,1);assert.equal(row.diagnostics.deletedLikeElements,1);
});
await test('SNS local optimistic comments and likes cannot acknowledge a send or claim complete state',async()=>{
  const row=await detailsInteractions(syntheticComment('71')+syntheticComment('72','','<b_deleted>0</b_deleted><is_local_added>1</is_local_added>'),
    '<user_comment><b_deleted>0</b_deleted><is_local_added>1</is_local_added><username>wxid_a</username></user_comment>');
  assert.equal(row.commentsVerified,false);assert.equal(row.likesVerified,false);assert.deepEqual(normalize(row.commentsDetail),[]);assert.deepEqual(normalize(row.likesDetail),[]);
  assert.equal(row.diagnostics.localPendingCommentElements,1);assert.equal(row.diagnostics.localPendingLikeElements,1);
});
await test('SNS missing or unsupported native flags never acknowledge comments or likes',async()=>{
  for(const flags of ['', '<b_deleted>0</b_deleted>','<b_deleted>false</b_deleted><is_local_added>0</is_local_added>']) {
    const row=await detailsInteractions(syntheticComment('71','',flags),'<user_comment>'+flags+'<username>wxid_a</username></user_comment>');
    assert.equal(row.commentsVerified,false);assert.equal(row.likesVerified,false);assert.deepEqual(normalize(row.commentsDetail),[]);assert.deepEqual(normalize(row.likesDetail),[]);
    assert(!JSON.stringify(row.diagnostics).includes('false'));
  }
});
await test('SNS conflicting 32/64 reply IDs or referenced actor reject authoritative comment list',async()=>{
  for(const reference of ['<ref_comment_id>71</ref_comment_id><ref_comment_64id>99</ref_comment_64id>',
      '<ref_comment_id>71</ref_comment_id><ref_username>wxid_other</ref_username>']) {
    const row=await detailsInteractions(syntheticComment('71','<comment_64id>9007199254740993123</comment_64id>')+syntheticComment('72',reference));
    assert.equal(row.commentsVerified,false);assert.deepEqual(normalize(row.commentsDetail),[]);
  }
});
await test('SNS duplicate 32-bit aliases cannot silently map distinct 64-bit comments',async()=>{
  const row=await detailsInteractions(syntheticComment('71','<comment_64id>9007199254740993123</comment_64id>')+syntheticComment('71','<comment_64id>9007199254740993124</comment_64id>'));
  assert.equal(row.commentsVerified,false);assert.deepEqual(normalize(row.commentsDetail),[]);
});
await test('SNS SAX text and CDATA retain exact decoded XML entities without double decoding',async()=>{
  const content=syntheticDetailsHeader.replace('synthetic post','A&amp;B<![CDATA[<C>]]>&#x1F642; &amp;lt;');
  const f=fixture({detailsRows:[{tid:'-1',user_name:'wxid_a',content:content+'<LocalExtraInfo><comment_user_list/><like_user_list/></LocalExtraInfo>'}]});
  const res=await detailsRequest(f);assert.equal(res.status,200);assert.equal(res.body.timeline[0].contentDesc,'A&B<C>🙂 &lt;');
});
await test('SNS details account switches and pending RPCs block old replies and additional SNS reads',async()=>{
  const f=fixture();f.wcdb.execQuery=async()=>{f.chat.accountCacheEpoch++;return {success:true,rows:[]}};
  assert.equal((await detailsRequest(f)).status,503);
  let timeout,release,reads=0;
  const g=fixture({deps:{setTimeout(callback){timeout=callback;return 1},clearTimeout(){}}});
  g.wcdb.execQuery=()=>{reads++;return new Promise(resolve=>{release=resolve})};
  const params={postIds:['18446744073709551615']},first=g.chat.getSnsIdentityDetails632(params);await tick();
  assert.equal(g.chat.getSnsIdentityDetails632(params),first);assert.equal((await schemaRequest(g)).status,503);assert.equal((await snsRequest(g)).status,503);
  timeout();assert.equal((await first).success,false);
  assert.equal((await detailsRequest(g,'?post_ids=1')).status,503);assert.equal(reads,1);
  release({success:true,rows:[]});await tick();
});

// Optional exact-bundle routing check evaluates only handleRequest with pure mocks.
// All other 2.7 MB of main.js is read as source but never executed.
const preparedDirectory=process.argv[2] || process.env.WEFLOW_PREPARED_DIR;
if (!preparedDirectory) throw new Error('Pass the prepared directory as argument 2 or set WEFLOW_PREPARED_DIR');
const preparedPath=path.join(preparedDirectory,'modified/dist-electron/main.js');
if(fs.existsSync(preparedPath)){
  const original=recorded.main,prepared=fs.readFileSync(preparedPath,'utf8');
  assert.equal(crypto.createHash('sha256').update(original).digest('hex'),'4d504f70c1c29041b3730df30a7aeac20fc213ce53d8d2585eb862c2f8ec36ec');
  const manifest=JSON.parse(fs.readFileSync(path.join(preparedDirectory,'operations.json'),'utf8'));
  assert.equal(prepared,original.replace(manifest.route_old,manifest.route_new)+'\n;(()=>{\n'+extension+'\n__wf632InstallIdentityExtension({chatService:X,wcdbService:B,httpService:hM,messagePushService:_M,crypto:o,fs:c,path:t,SaxesParser:require("saxes").SaxesParser});\n})();\n');
  const start=prepared.indexOf('async handleRequest('),end=prepared.indexOf('startMessagePushHeartbeat(){',start);
  assert(start>=0&&end>start&&end-start<5000);
  const method=vm.runInNewContext('({'+prepared.slice(start,end)+'}).handleRequest',{s:{URL},console},{timeout:1000});
  await test('exact 6.3.2 route keeps original token guard, OPTIONS and old contacts behavior',async()=>{
    const f=fixture();Object.assign(f.http,{host:'127.0.0.1',port:10086,parseBody:async()=>({}),verifyToken:req=>req.headers.authorization==='Bearer synthetic-test',
      async handleContacts(url,res){res.status=200;res.body={legacy:true}},handleRequest:method});
    const req=(method='GET',url='/api/v1/identity-snapshot',auth='Bearer synthetic-test')=>({method,url,headers:{authorization:auth}});
    let res=response();await f.http.handleRequest(req('GET',undefined,''),res);assert.equal(res.status,401);assert.equal(f.calls.sql.length,0);
    res=response();await f.http.handleRequest(req('POST'),res);assert.equal(res.status,405);assert.equal(f.calls.sql.length,0);
    res=response();await f.http.handleRequest(req('OPTIONS'),res);assert.equal(res.status,204);assert.equal(f.calls.sql.length,0);
    res=response();await f.http.handleRequest(req(),res);assert.equal(res.status,200);assert.equal(res.body.fresh,true);assert.equal(f.calls.sql.length,1);
    res=response();await f.http.handleRequest(req('GET','/api/v1/contacts'),res);assert.equal(res.body.legacy,true);assert.equal(f.calls.sql.length,1);
    res=response();await f.http.handleRequest(req('GET','/api/v1/sns-identity-timeline?limit=1',''),res);assert.equal(res.status,401);assert.equal(f.calls.sns.length,0);
    res=response();await f.http.handleRequest(req('POST','/api/v1/sns-identity-timeline'),res);assert.equal(res.status,405);assert.equal(f.calls.sns.length,0);
    res=response();await f.http.handleRequest(req('GET','/api/v1/sns-identity-timeline?limit=1'),res);assert.equal(res.status,200);assert.equal(res.body.capabilities.snsIdentityTimeline,1);assert.equal(f.calls.sns.length,1);
    res=response();await f.http.handleRequest(req('GET','/api/v1/sns-identity-schema',''),res);assert.equal(res.status,401);assert.equal(f.calls.tables.length,0);
    res=response();await f.http.handleRequest(req('POST','/api/v1/sns-identity-schema'),res);assert.equal(res.status,405);assert.equal(f.calls.tables.length,0);
    res=response();await f.http.handleRequest(req('GET','/api/v1/sns-identity-schema'),res);assert.equal(res.status,200);assert.equal(res.body.capabilities.snsIdentitySchema,1);assert.equal(f.calls.tables.length,1);
    const beforeDetails=f.calls.sql.length;
    res=response();await f.http.handleRequest(req('GET','/api/v1/sns-identity-details?post_ids=18446744073709551615',''),res);assert.equal(res.status,401);assert.equal(f.calls.sql.length,beforeDetails);
    res=response();await f.http.handleRequest(req('POST','/api/v1/sns-identity-details'),res);assert.equal(res.status,405);assert.equal(f.calls.sql.length,beforeDetails);
    res=response();await f.http.handleRequest(req('GET','/api/v1/sns-identity-details?post_ids=18446744073709551615'),res);assert.equal(res.status,200);assert.equal(res.body.capabilities.snsIdentityDetails,1);assert.equal(f.calls.sql.length,beforeDetails+1);
  });
  const workerSource=recorded.worker;
  await test('exact B service and native worker methods preserve timeline rows without synthesized dependency assumptions',async()=>{
    assert.equal((original.match(/B=new on/g)||[]).length,1);
    const serviceStart=original.indexOf('async getSnsTimeline('),serviceEnd=original.indexOf('async getSnsAnnualStats(',serviceStart);
    assert(serviceStart>=0&&serviceEnd>serviceStart&&serviceEnd-serviceStart<300);
    const serviceMethod=vm.runInNewContext('({'+original.slice(serviceStart,serviceEnd)+'}).getSnsTimeline',{}, {timeout:1000});
    let called;
    await serviceMethod.call({callWorker(name,args){called={name,args};return {success:true,timeline:[]}}},20,0,['wxid_a']);
    assert.equal(called.name,'getSnsTimeline');assert.deepEqual(normalize(called.args),{limit:20,offset:0,usernames:['wxid_a']});
    const nativeStart=workerSource.indexOf('async getSnsTimeline('),nativeEnd=workerSource.indexOf('async getSnsAnnualStats(',nativeStart);
    assert(nativeStart>=0&&nativeEnd>nativeStart&&nativeEnd-nativeStart<1000);
    const nativeMethod=vm.runInNewContext('({'+workerSource.slice(nativeStart,nativeEnd)+'}).getSnsTimeline',{}, {timeout:1000});
    const data=[{id:'123',username:'wxid_a',comments:[{id:'8',username:'wxid_b',refUsername:'wxid_a'}]}];
    const owner={handle:1,ensureReady(){return true},wcdbGetSnsTimeline(...args){args[7][0]='synthetic-pointer';return 0},decodeJsonPtr(){return JSON.stringify(data)}};
    const result=await nativeMethod.call(owner,20,0,['wxid_a']);assert.deepEqual(normalize(result),{success:true,timeline:data});
  });
  await test('exact B metadata service/worker contracts use fixed SNS kind, blank path and native SQL string',async()=>{
    for(const name of ['listTables','getTableSchema']) {
      const marker='async '+name+'(',start=original.indexOf(marker),end=original.indexOf('async ',start+6);
      assert(start>=0&&end>start&&end-start<300);
      const method=vm.runInNewContext('({'+original.slice(start,end)+'}).'+name,{}, {timeout:1000});
      let called;const args=name==='listTables'?['sns','']:['sns','','SnsTimeLine'];
      await method.call({callWorker(workerName,parameters){called={workerName,parameters};return {success:true}}},...args);
      assert.equal(called.workerName,name);
      assert.deepEqual(normalize(called.parameters),name==='listTables'?{kind:'sns',dbPath:''}:{kind:'sns',dbPath:'',tableName:'SnsTimeLine'});
      const nativeStart=workerSource.indexOf(marker),nativeEnd=workerSource.indexOf('async ',nativeStart+6);
      assert(nativeStart>=0&&nativeEnd>nativeStart&&nativeEnd-nativeStart<1200);
      const native=vm.runInNewContext('({'+workerSource.slice(nativeStart,nativeEnd)+'}).'+name,{}, {timeout:1000});
      const schema='CREATE TABLE SnsTimeLine (tid INTEGER, content BLOB)';let nativeArgs;
      const owner={handle:42,ensureReady(){return true},decodeJsonPtr(){return name==='listTables'?JSON.stringify(['SnsTimeLine']):JSON.stringify({success:true,schema})},
        wcdbListTables(...a){nativeArgs=a;a[3][0]='synthetic-pointer';return 0},
        wcdbGetTableSchema(...a){nativeArgs=a;a[4][0]='synthetic-pointer';return 0}};
      const result=await native.call(owner,...args);
      assert.deepEqual(normalize(nativeArgs.slice(0,-1)),[42,...args]);
      assert.deepEqual(normalize(result),name==='listTables'?{success:true,tables:['SnsTimeLine']}:{success:true,schema});
    }
  });
}else throw new Error('Prepared main.js missing; run prepare_weflow_632.py with an explicit output directory first');
console.log('Pure-mock checks passed: '+checks+'; no installed files/native execution/network requests.');
