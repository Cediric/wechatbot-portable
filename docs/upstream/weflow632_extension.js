// Readable adapter for the exact WeFlow 6.3.2 bundle recorded in the manifest.
// Defines a factory only. Loading this file alone never opens a DB or changes UI.
function __wf632InstallIdentityExtension(deps) {
  "use strict";
  const {chatService, wcdbService, httpService, messagePushService, crypto, fs, path, SaxesParser} = deps;
  const setDeadline = deps.setTimeout || setTimeout;
  const clearDeadline = deps.clearTimeout || clearTimeout;
  const required = [[chatService,"ensureConnected"], [chatService,"getCacheAccountScope"],
    [chatService,"isAccountCacheContextCurrent"], [wcdbService,"execQuery"],
    [wcdbService,"getSessions"], [wcdbService,"getSnsTimeline"], [wcdbService,"listTables"],
    [wcdbService,"getTableSchema"], [httpService,"sendJson"], [httpService,"sendError"],
    [httpService,"sendMethodNotAllowed"], [httpService,"toSnsMediaKey"], [messagePushService,"buildPayload"], [crypto,"createHash"]];
  for (const [owner, name] of required) {
    if (!owner || typeof owner[name] !== "function") throw new Error("6.3.2 identity extension: missing " + name);
  }
  if (chatService.getFreshIdentitySnapshot || httpService.handleFreshIdentitySnapshot632 ||
      chatService.getSnsIdentityTimeline632 || httpService.handleSnsIdentityTimeline632 ||
      chatService.getSnsIdentitySchema632 || httpService.handleSnsIdentitySchema632 ||
      chatService.getSnsIdentityDetails632 || httpService.handleSnsIdentityDetails632) {
    throw new Error("6.3.2 identity extension is already installed or incompatible");
  }
  const validIdentity = value => typeof value === "string" &&
    /^[A-Za-z0-9_.@:-]{1,256}$/.test(value) && !/^(?:unknown|null|None)$/.test(value) && value !== "@chatroom";
  // Native SNS keys are uint64 decimal strings. The original HTTP adapter's
  // Number() conversion rounds large keys before SnsService can decrypt them.
  const originalMediaKey = httpService.toSnsMediaKey;
  httpService.toSnsMediaKey = function(value) {
    if (typeof value === "string" && /^-?\d+$/.test(value.trim())) return value.trim();
    return originalMediaKey.call(this,value);
  };
  function field(row, aliases) {
    if (!row || typeof row !== "object" || Array.isArray(row)) return "";
    for (const alias of aliases) {
      if (typeof row[alias] === "string" && row[alias] !== "") return row[alias];
    }
    const keys = new Map(Object.keys(row).map(key => [key.toLowerCase(),key]));
    for (const alias of aliases) {
      const key = keys.get(alias.toLowerCase());
      if (key && typeof row[key] === "string" && row[key] !== "") return row[key];
    }
    return "";
  }
  async function readSnapshot() {
    try {
      const connected = await chatService.ensureConnected();
      if (!connected || connected.success !== true) return {success:false,error:connected?.error || "WCDB unavailable"};
      const scope = chatService.getCacheAccountScope(), epoch = chatService.accountCacheEpoch;
      const current = () => chatService.isAccountCacheContextCurrent(scope,epoch);
      if (!current()) return {success:false,error:"Account changed during identity snapshot"};
      const contactsResult = await wcdbService.execQuery("contact",null,"SELECT * FROM contact");
      if (!current()) return {success:false,error:"Account changed during identity snapshot"};
      if (!contactsResult || contactsResult.success !== true || !Array.isArray(contactsResult.rows)) {
        return {success:false,error:contactsResult?.error || "Fresh contact.db query unavailable"};
      }
      const sessionsResult = await wcdbService.getSessions();
      if (!current()) return {success:false,error:"Account changed during identity snapshot"};
      if (!sessionsResult || sessionsResult.success !== true || !Array.isArray(sessionsResult.sessions)) {
        return {success:false,error:sessionsResult?.error || "Fresh sessions unavailable"};
      }
      const contacts = [], sessions = [], names = new Map(), seenContacts = new Set(), seenSessions = new Set();
      for (const row of contactsResult.rows) {
        const username = field(row,["username","user_name","userName"]);
        if (!validIdentity(username)) continue;
        if (seenContacts.has(username)) return {success:false,error:"Duplicate contact identity"};
        seenContacts.add(username);
        const remark = field(row,["remark","WCDB_CT_remark"]);
        const nickname = field(row,["nick_name","nickName","nickname","NickName","WCDB_CT_nick_name"]);
        const alias = field(row,["alias","WCDB_CT_alias"]);
        const displayName = remark || nickname || alias || username;
        contacts.push({username,displayName,remark,nickname,alias,type:username.endsWith("@chatroom")?"group":"friend"});
        names.set(username,displayName);
      }
      for (const row of sessionsResult.sessions) {
        // These are the raw aliases used by installed 6.3.2 ChatService.getSessions.
        const username = field(row,["username","user_name","userName","usrName","UsrName","talker","talker_id","talkerId"]);
        if (!validIdentity(username)) continue;
        if (seenSessions.has(username)) return {success:false,error:"Duplicate session identity"};
        seenSessions.add(username);
        sessions.push({username,displayName:names.get(username)||username,type:username.endsWith("@chatroom")?2:1});
      }
      contacts.sort((a,b)=>a.username.localeCompare(b.username));
      sessions.sort((a,b)=>a.username.localeCompare(b.username));
      return {success:true,contacts,sessions};
    } catch (error) {
      return {success:false,error:String(error)};
    }
  }
  let inFlight = null;
  chatService.getFreshIdentitySnapshot = function() {
    if (inFlight) return inFlight;
    const work = readSnapshot();
    let timer;
    const reply = new Promise(resolve => {
      timer = setDeadline(()=>resolve({success:false,error:"Fresh identity query timed out"}),2500);
      void work.then(result=>{clearDeadline(timer);resolve(result)},error=>{
        clearDeadline(timer);resolve({success:false,error:String(error)});
      });
    });
    inFlight = reply;
    // The deadline never cancels a native RPC; don't enqueue another while hung.
    void work.finally(()=>{clearDeadline(timer);if(inFlight===reply)inFlight=null}).catch(()=>{});
    return reply;
  };
  httpService.handleFreshIdentitySnapshot632 = async function(req,res) {
    if (req.method !== "GET") return this.sendMethodNotAllowed(res,"GET");
    const result = await chatService.getFreshIdentitySnapshot();
    if (result.success !== true) return this.sendError(res,503,result.error || "Fresh identity snapshot unavailable");
    const revision = crypto.createHash("sha256").update(JSON.stringify({contacts:result.contacts,sessions:result.sessions})).digest("hex");
    const etag = '"' + revision + '"';
    res.setHeader("ETag",etag);
    res.setHeader("Cache-Control","no-store");
    res.setHeader("X-WeFlow-Identity-Snapshot","1");
    if (req.headers["if-none-match"] === etag) {res.writeHead(304);res.end();return;}
    this.sendJson(res,{success:true,version:1,fresh:true,capabilities:{freshIdentitySnapshot:1,snsMediaKey:1},
      snapshotAt:Date.now(),revision,contacts:result.contacts,sessions:result.sessions});
  };

  // Read native SNS rows before SnsService.fixCommentRefs drops actor fields.
  // This is a local-cache read; it never claims to refresh WeChat's server feed.
  const snsParameters = url => {
    const allowed = new Set(["access_token","limit","offset","usernames"]);
    for (const key of url.searchParams.keys()) {
      if (!allowed.has(key) || url.searchParams.getAll(key).length !== 1) {
        throw new Error("Unsupported or duplicate SNS query parameter");
      }
    }
    const integer = (key, fallback, minimum, maximum) => {
      const raw = url.searchParams.get(key);
      if (raw === null) return fallback;
      if (!/^(?:0|[1-9][0-9]{0,5})$/.test(raw)) throw new Error("Invalid SNS " + key);
      const value = Number(raw);
      if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new Error("Invalid SNS " + key);
      return value;
    };
    const filter = url.searchParams.get("usernames");
    if (filter !== null && filter.length > 16384) throw new Error("SNS username filter too large");
    const usernames = filter ? Array.from(new Set(filter.split(",").map(value => value.trim()).filter(Boolean))) : [];
    if (usernames.length > 100 || usernames.some(value => !validIdentity(value))) throw new Error("Invalid SNS username filter");
    return {limit:integer("limit",20,1,100),offset:integer("offset",0,0,200),usernames};
  };
  async function readSnsTimeline(parameters) {
    try {
      const connected = await chatService.ensureConnected();
      if (!connected || connected.success !== true) return {success:false,error:"SNS database unavailable"};
      const scope = chatService.getCacheAccountScope(), epoch = chatService.accountCacheEpoch;
      const current = () => chatService.isAccountCacheContextCurrent(scope,epoch);
      if (!current()) return {success:false,error:"Account changed during SNS read"};
      const result = await wcdbService.getSnsTimeline(parameters.limit,parameters.offset,
        parameters.usernames.length ? parameters.usernames : undefined,undefined,undefined,undefined);
      if (!current()) return {success:false,error:"Account changed during SNS read"};
      if (!result || result.success !== true || !Array.isArray(result.timeline) ||
          result.timeline.length > parameters.limit || result.timeline.some(row => !row || typeof row !== "object" || Array.isArray(row))) {
        return {success:false,error:"Native SNS timeline unavailable or invalid"};
      }
      // Never synthesize comment IDs or turn a display name into a username.
      // All row/actor/ref fields and rawXml are retained exactly as native data.
      if (Buffer.byteLength(JSON.stringify(result.timeline),"utf8") > 8 * 1024 * 1024) {
        return {success:false,error:"Native SNS timeline exceeds response limit"};
      }
      return {success:true,timeline:result.timeline};
    } catch (error) {
      return {success:false,error:"Native SNS timeline read failed"};
    }
  }
  let snsInFlight = null;
  function singleSnsRead(key,reader) {
    if (snsInFlight) {
      if (snsInFlight.key === key) return snsInFlight.reply;
      return Promise.resolve({success:false,error:"Another native SNS read is still running"});
    }
    const work = reader();
    let timer;
    const reply = new Promise(resolve => {
      timer = setDeadline(()=>resolve({success:false,error:"Native SNS read timed out"}),2500);
      void work.then(result=>{clearDeadline(timer);resolve(result)},()=>{
        clearDeadline(timer);resolve({success:false,error:"Native SNS read failed"});
      });
    });
    const owned = {key,reply};
    snsInFlight = owned;
    // Even after the reply deadline, retain the same failure until the actual
    // native call ends. A different offset/filter cannot enqueue more RPCs.
    void work.finally(()=>{clearDeadline(timer);if(snsInFlight===owned)snsInFlight=null}).catch(()=>{});
    return reply;
  }
  chatService.getSnsIdentityTimeline632 = function(parameters) {
    const key = JSON.stringify(["timeline",chatService.getCacheAccountScope(),chatService.accountCacheEpoch,
      parameters.limit,parameters.offset,parameters.usernames]);
    return singleSnsRead(key,()=>readSnsTimeline(parameters));
  };
  httpService.handleSnsIdentityTimeline632 = async function(req,url,res) {
    if (req.method !== "GET") return this.sendMethodNotAllowed(res,"GET");
    let parameters;
    try {parameters=snsParameters(url);} catch (error) {return this.sendError(res,400,String(error.message));}
    const result = await chatService.getSnsIdentityTimeline632(parameters);
    if (result.success !== true) return this.sendError(res,503,result.error || "Native SNS timeline unavailable");
    res.setHeader("Cache-Control","no-store");
    res.setHeader("X-WeFlow-SNS-Identity-Timeline","1");
    this.sendJson(res,{success:true,version:1,capabilities:{snsIdentityTimeline:1},
      count:result.timeline.length,timeline:result.timeline});
  };

  // The exact local native DLL references SnsTimeLine, not a guessed SnsInfo.
  // Unknown table names may be listed but can never select another schema.
  const snsSchemaTables = new Set(["SnsTimeLine"]);
  const schemaName = value => typeof value === "string" && /^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(value);
  function currentSnsDbPath() {
    // Generic native metadata does not locate SNS automatically in this build.
    // This exact filename exists beneath the selected account on the real host.
    // It can never be supplied by an HTTP parameter or escape that account.
    if (!fs || !path || typeof fs.realpathSync !== "function" || typeof fs.statSync !== "function" ||
        typeof path.join !== "function" || typeof path.relative !== "function" || typeof path.isAbsolute !== "function" ||
        !chatService.configService || typeof chatService.configService.get !== "function" ||
        typeof chatService.configService.getAccountDir !== "function") throw new Error("SNS path resolver unavailable");
    const base=String(chatService.runtimeConfig?.dbPath || chatService.configService.get("dbPath") || "").trim();
    const username=String(chatService.runtimeConfig?.myWxid || chatService.configService.get("myWxid") || "").trim();
    if (!base || !username) throw new Error("SNS account unavailable");
    const account=chatService.configService.getAccountDir(base,username);
    if (!account) throw new Error("SNS account unavailable");
    const root=fs.realpathSync(account),file=fs.realpathSync(path.join(root,"db_storage","sns","sns.db"));
    const relative=path.relative(root,file);
    if (!relative || relative === ".." || relative.startsWith(".."+path.sep) || path.isAbsolute(relative) ||
        relative.replace(/\\/g,"/").toLowerCase() !== "db_storage/sns/sns.db" || !fs.statSync(file).isFile()) {
      throw new Error("SNS database path is outside the selected account");
    }
    return file;
  }
  function schemaColumns(sql,tableName) {
    if (typeof sql !== "string" || sql.length > 65536) throw new Error("Invalid SNS schema");
    const header = /^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"([A-Za-z_][A-Za-z0-9_]*)"|`([A-Za-z_][A-Za-z0-9_]*)`|\[([A-Za-z_][A-Za-z0-9_]*)\]|([A-Za-z_][A-Za-z0-9_]*))\s*\(/i.exec(sql);
    if (!header || header.slice(1).find(Boolean) !== tableName) throw new Error("Invalid SNS schema");
    const definitions = [];
    let depth=1,quote="",start=header[0].length,end=-1;
    for (let index=start;index<sql.length;index++) {
      const char=sql[index];
      if (quote) {
        const closing=quote === "[" ? "]" : quote;
        if (char === closing) {
          if (sql[index+1] === closing && quote !== "[") index++;
          else quote="";
        }
        continue;
      }
      if (char === "'" || char === '"' || char === "`" || char === "[") {quote=char;continue;}
      if (char === "(") depth++;
      else if (char === ")") {
        if (--depth === 0) {definitions.push(sql.slice(start,index).trim());end=index+1;break;}
      } else if (char === "," && depth === 1) {
        definitions.push(sql.slice(start,index).trim());start=index+1;
      }
    }
    if (end < 0 || quote || !/^\s*(?:(?:WITHOUT\s+ROWID|STRICT)(?:\s*,\s*(?:WITHOUT\s+ROWID|STRICT))?)?\s*;?\s*$/i.test(sql.slice(end)) ||
        definitions.length > 256 || definitions.some(value=>!value)) throw new Error("Invalid SNS schema");
    const columns=[],seen=new Set();
    for (const definition of definitions) {
      if (/^(?:CONSTRAINT|PRIMARY|UNIQUE|CHECK|FOREIGN)\b/i.test(definition)) continue;
      const named=/^(?:"([A-Za-z_][A-Za-z0-9_]*)"|`([A-Za-z_][A-Za-z0-9_]*)`|\[([A-Za-z_][A-Za-z0-9_]*)\]|([A-Za-z_][A-Za-z0-9_]*))(?:\s+|$)(.*)$/s.exec(definition);
      if (!named) throw new Error("Invalid SNS schema column");
      const name=named.slice(1,5).find(Boolean),rest=named[5];
      const type=rest.split(/\b(?:CONSTRAINT|PRIMARY|NOT|UNIQUE|CHECK|DEFAULT|COLLATE|REFERENCES|GENERATED|AS)\b/i,1)[0].trim().replace(/\s+/g," ").toUpperCase();
      if (!schemaName(name) || seen.has(name.toLowerCase()) ||
          (type && !/^(?:INT|INTEGER|TINYINT|SMALLINT|MEDIUMINT|BIGINT|UNSIGNED BIG INT|INT2|INT8|CHARACTER|VARCHAR|VARYING CHARACTER|NCHAR|NATIVE CHARACTER|NVARCHAR|TEXT|CLOB|BLOB|REAL|DOUBLE|DOUBLE PRECISION|FLOAT|NUMERIC|DECIMAL|BOOLEAN|DATE|DATETIME)(?:\s*\(\s*[0-9]{1,4}\s*(?:,\s*[0-9]{1,4}\s*)?\))?$/.test(type))) {
        throw new Error("Invalid SNS schema column");
      }
      seen.add(name.toLowerCase());columns.push({name,type});
    }
    if (!columns.length || columns.length > 128) throw new Error("Invalid SNS schema columns");
    return columns;
  }
  async function readSnsSchema() {
    try {
      const connected=await chatService.ensureConnected();
      if (!connected || connected.success !== true) return {success:false,error:"SNS database unavailable"};
      const scope=chatService.getCacheAccountScope(),epoch=chatService.accountCacheEpoch;
      const current=()=>chatService.isAccountCacheContextCurrent(scope,epoch);
      if (!current()) return {success:false,error:"Account changed during SNS schema read"};
      // Arguments proven against the recorded B service and native worker.
      let dbPath="",listed=await wcdbService.listTables("sns",dbPath);
      if (!current()) return {success:false,error:"Account changed during SNS schema read"};
      if (listed?.success === true && Array.isArray(listed.tables) && listed.tables.length === 0) {
        dbPath=currentSnsDbPath();
        if (!current()) return {success:false,error:"Account changed during SNS schema read"};
        listed=await wcdbService.listTables("sns",dbPath);
        if (!current()) return {success:false,error:"Account changed during SNS schema read"};
      }
      if (!listed || listed.success !== true || !Array.isArray(listed.tables) || listed.tables.length > 128 ||
          listed.tables.some(name=>!schemaName(name)) || new Set(listed.tables).size !== listed.tables.length) {
        return {success:false,error:"Native SNS table metadata unavailable"};
      }
      const tableNames=[...listed.tables].sort(),tables=[];
      for (const name of tableNames) {
        if (!snsSchemaTables.has(name)) continue;
        const result=await wcdbService.getTableSchema("sns",dbPath,name);
        if (!current()) return {success:false,error:"Account changed during SNS schema read"};
        if (!result || result.success !== true) return {success:false,error:"Native SNS column metadata unavailable"};
        tables.push({name,columns:schemaColumns(result.schema,name)});
      }
      return {success:true,tableNames,tables};
    } catch (error) {
      // Never send raw native errors, SQL, DEFAULT literals or file paths.
      return {success:false,error:"Native SNS schema metadata unavailable"};
    }
  }
  chatService.getSnsIdentitySchema632 = function() {
    const key=JSON.stringify(["schema",chatService.getCacheAccountScope(),chatService.accountCacheEpoch]);
    return singleSnsRead(key,readSnsSchema);
  };
  httpService.handleSnsIdentitySchema632 = async function(req,url,res) {
    if (req.method !== "GET") return this.sendMethodNotAllowed(res,"GET");
    for (const key of url.searchParams.keys()) {
      if (key !== "access_token" || url.searchParams.getAll(key).length !== 1) {
        return this.sendError(res,400,"Unsupported or duplicate SNS schema query parameter");
      }
    }
    const result=await chatService.getSnsIdentitySchema632();
    if (result.success !== true) return this.sendError(res,503,result.error || "Native SNS schema metadata unavailable");
    res.setHeader("Cache-Control","no-store");
    res.setHeader("X-WeFlow-SNS-Identity-Schema","1");
    this.sendJson(res,{success:true,version:1,capabilities:{snsIdentitySchema:1},tableNames:result.tableNames,tables:result.tables});
  };
  function nativeTid(row) {
    if (!row || typeof row.tid !== "string" || !/^-?(?:0|[1-9][0-9]{0,19})$/.test(row.tid) ||
        typeof row.id !== "string" || !/^[1-9][0-9]{0,19}$/.test(row.id) || !validIdentity(row.username)) throw new Error("Invalid SNS identity");
    const value=BigInt(row.tid);
    if (value < -(1n<<63n) || value >= (1n<<63n) || BigInt.asUintN(64,value).toString() !== row.id) throw new Error("SNS native identity mismatch");
    return row.tid;
  }
  function parseSnsContent(content,row) {
    if (typeof SaxesParser !== "function" || typeof content !== "string" || content.length > 8*1024*1024 ||
        /<!\s*(?:DOCTYPE|ENTITY)\b/i.test(content) || content.includes("\0")) throw new Error("Invalid SNS XML");
    const root={name:"",children:[],text:""},stack=[root];let nodes=0;
    const parser=new SaxesParser({fragment:true,xmlns:false});
    parser.on("error",()=>{throw new Error("Invalid SNS XML")});
    parser.on("doctype",()=>{throw new Error("Invalid SNS XML")});
    parser.on("processinginstruction",()=>{throw new Error("Invalid SNS XML")});
    parser.on("opentag",tag=>{
      if (++nodes>50000 || stack.length>64) throw new Error("SNS XML exceeds structure limit");
      const node={name:tag.name,children:[],text:""};stack[stack.length-1].children.push(node);stack.push(node);
    });
    const append=value=>{stack[stack.length-1].text+=value};
    parser.on("text",append);parser.on("cdata",append);parser.on("closetag",()=>stack.pop());
    const xmlShape=()=>({rootElements:root.children.map(node=>node.name),firstLevelElements:[...new Set(root.children.flatMap(node=>node.children.map(item=>item.name)))].slice(0,100)});
    try {parser.write(content).close();} catch(error) {const failure=new Error("Invalid SNS XML");failure.snsXmlShape=xmlShape();throw failure;}
    if (stack.length!==1) throw new Error("Invalid SNS XML");
    const child=(node,name)=>{
      const found=node.children.filter(item=>item.name===name);
      if (found.length>1) throw new Error("Duplicate SNS XML field");
      return found[0];
    };
    const scalar=(node,names)=>{
      const found=names.map(name=>child(node,name)).filter(Boolean);
      if (found.length>1 || found.some(item=>item.children.length)) throw new Error("Duplicate or nested SNS identity field");
      return found[0]?.text || "";
    };
    const wrapper=child(root,"SnsDataItem");
    if (wrapper && root.children.length!==1) throw new Error("Duplicate SNS XML source");
    const container=wrapper || root;
    const timeline=child(container,"TimelineObject");
    if (!timeline || scalar(timeline,["id"])!==row.id || scalar(timeline,["username"])!==row.username) {
      const failure=new Error("SNS database XML identity mismatch");failure.snsXmlShape=xmlShape();throw failure;
    }
    const rootExtra=child(container,"LocalExtraInfo"),nestedExtra=child(timeline,"LocalExtraInfo");
    if (rootExtra && nestedExtra) throw new Error("Duplicate SNS extra info");
    const extra=rootExtra || nestedExtra;
    const commentList=extra ? child(extra,"comment_user_list") : undefined;
    const likeList=extra ? child(extra,"like_user_list") : undefined;
    const commentNodes=commentList?.children.filter(item=>item.name==="user_comment") || [];
    const likeNodes=likeList?.children.filter(item=>item.name==="user_comment") || [];
    if (commentNodes.length>500 || likeNodes.length>1000 ||
        (commentList && commentList.children.length!==commentNodes.length) || (likeList && likeList.children.length!==likeNodes.length)) throw new Error("Invalid SNS interactions");
    const comments=[],likes=[],commentIds=new Set(),likeIds=new Set();
    let commentsVerified=!!commentList,likesVerified=!!likeList;
    let deletedComments=0,pendingComments=0,deletedLikes=0,pendingLikes=0,commentsWith64Id=0;
    const positiveId=value=>/^[1-9][0-9]{0,19}$/.test(value) && BigInt(value)<(1n<<64n);
    const flagKinds={commentDeleted:new Set(),commentLocalAdded:new Set(),likeDeleted:new Set(),likeLocalAdded:new Set()};
    const flagKind=value=>value==="0" || value==="1"?value:value===""?"absent-or-empty":"unsupported";
    for (const node of commentNodes) {
      const deleted=scalar(node,["b_deleted"]),pending=scalar(node,["is_local_added"]);
      flagKinds.commentDeleted.add(flagKind(deleted));flagKinds.commentLocalAdded.add(flagKind(pending));
      if (deleted==="1") {deletedComments++;continue;}
      if (pending==="1") {pendingComments++;commentsVerified=false;continue;}
      if (deleted!=="0" || pending!=="0") {commentsVerified=false;continue;}
      const commentId=scalar(node,["cmtid","comment_id","id"]),comment64Id=scalar(node,["comment_64id"]);
      const id=comment64Id && comment64Id!=="0"?comment64Id:commentId,username=scalar(node,["username"]);
      const refCommentId32=scalar(node,["ref_comment_id"]),refCommentId64=scalar(node,["ref_comment_64id"]);
      const refCommentId=refCommentId64 && refCommentId64!=="0"?refCommentId64:refCommentId32,refUsername=scalar(node,["ref_username"]);
      // Every returned comment must carry native XML identity; no display-name inference.
      if (!positiveId(id) || (commentId && commentId!=="0" && !positiveId(commentId)) || !validIdentity(username) || commentIds.has(id) ||
          (refCommentId && refCommentId!=="0" && !positiveId(refCommentId)) ||
          (refCommentId32 && refCommentId32!=="0" && !positiveId(refCommentId32)) || (refUsername && !validIdentity(refUsername))) {
        commentsVerified=false;continue;
      }
      commentIds.add(id);
      if (comment64Id && comment64Id!=="0") commentsWith64Id++;
      const rawTime=scalar(node,["createTime","create_time"]);
      const createTime=/^[0-9]{1,12}$/.test(rawTime)?Number(rawTime):0;
      comments.push({id,username,nickname:scalar(node,["nickname"]),content:scalar(node,["content"]),
        refCommentId:refCommentId==="0"?"":refCommentId,refUsername,refNickname:scalar(node,["ref_nickname"]),createTime,
        commentId:commentId==="0"?"":commentId,comment64Id:comment64Id==="0"?"":comment64Id,
        refCommentId32:refCommentId32==="0"?"":refCommentId32,refCommentId64:refCommentId64==="0"?"":refCommentId64});
    }
    const canonicalIds=new Map(comments.filter(item=>item.commentId).map(item=>[item.commentId,item.id]));
    if (canonicalIds.size!==comments.filter(item=>item.commentId).length) commentsVerified=false;
    const commentsById=new Map(comments.map(item=>[item.id,item]));
    for (const item of comments) {
      const mapped=item.refCommentId32?canonicalIds.get(item.refCommentId32):undefined;
      if (mapped && item.refCommentId64 && mapped!==item.refCommentId64) commentsVerified=false;
      if (mapped && !item.refCommentId64) item.refCommentId=mapped;
      const referenced=commentsById.get(item.refCommentId);
      if (referenced && item.refUsername && referenced.username!==item.refUsername) commentsVerified=false;
    }
    for (const node of likeNodes) {
      const deleted=scalar(node,["b_deleted"]),pending=scalar(node,["is_local_added"]);
      flagKinds.likeDeleted.add(flagKind(deleted));flagKinds.likeLocalAdded.add(flagKind(pending));
      if (deleted==="1") {deletedLikes++;continue;}
      if (pending==="1") {pendingLikes++;likesVerified=false;continue;}
      if (deleted!=="0" || pending!=="0") {likesVerified=false;continue;}
      const username=scalar(node,["username"]);
      if (!validIdentity(username) || likeIds.has(username)) {likesVerified=false;continue;}
      likeIds.add(username);likes.push({username,nickname:scalar(node,["nickname"])});
    }
    if (!commentsVerified) comments.length=0;
    if (!likesVerified) likes.length=0;
    const rawCreated=scalar(timeline,["createTime"]);
    if (!/^[0-9]{1,12}$/.test(rawCreated)) throw new Error("Invalid SNS XML timestamp");
    return {tid:row.tid,id:row.id,username:row.username,contentDesc:scalar(timeline,["contentDesc"]),createTime:Number(rawCreated),identityVerified:true,
      commentsVerified,likesVerified,commentsDetail:comments,likesDetail:likes,
      diagnostics:{rootElements:root.children.map(item=>item.name),
        commentElementFields:[...new Set(commentNodes.flatMap(node=>node.children.map(item=>item.name)))].sort(),
        likeElementFields:[...new Set(likeNodes.flatMap(node=>node.children.map(item=>item.name)))].sort(),
        nativeCommentElements:commentNodes.length,nativeLikeElements:likeNodes.length,
        deletedCommentElements:deletedComments,localPendingCommentElements:pendingComments,
        deletedLikeElements:deletedLikes,localPendingLikeElements:pendingLikes,commentsWith64Id,
        flagValueKinds:Object.fromEntries(Object.entries(flagKinds).map(([key,value])=>[key,[...value].sort()]))}};
  }
  function detailsParameters(url) {
    for (const key of url.searchParams.keys()) {
      if ((key!=="access_token" && key!=="post_ids") || url.searchParams.getAll(key).length!==1) throw new Error("Unsupported or duplicate SNS details parameter");
    }
    const raw=url.searchParams.get("post_ids");
    if (!raw || raw.length>419) throw new Error("SNS details requires post_ids");
    const postIds=raw.split(",");
    if (postIds.length>20 || new Set(postIds).size!==postIds.length || postIds.some(id=>!/^([1-9][0-9]{0,19})$/.test(id) || BigInt(id)>((1n<<64n)-1n))) throw new Error("Invalid SNS post_ids");
    return {postIds};
  }
  async function readSnsDetails(parameters) {
    let stage="connect",diagnostics={};
    try {
      const connected=await chatService.ensureConnected();
      if (!connected || connected.success!==true) return {success:false,error:"SNS database unavailable"};
      const scope=chatService.getCacheAccountScope(),epoch=chatService.accountCacheEpoch;
      const current=()=>chatService.isAccountCacheContextCurrent(scope,epoch);
      if (!current()) return {success:false,error:"Account changed during SNS details read"};
      const tids=parameters.postIds.map(id=>BigInt.asIntN(64,BigInt(id)).toString());
      stage="path";
      const dbPath=currentSnsDbPath();
      if (!current()) return {success:false,error:"Account changed during SNS details read"};
      // Only strictly validated signed decimal IDs from the native response enter SQL.
      // SQL/table/columns/path are fixed server-side; pack_info_buf is never read.
      const sql="SELECT CAST(tid AS TEXT) AS tid, user_name, content FROM SnsTimeLine WHERE tid IN ("+
        tids.map(tid=>"'"+tid+"'").join(",")+") LIMIT 20";
      stage="query";
      const selected=await wcdbService.execQuery("sns",dbPath,sql);
      diagnostics={nativeSuccess:selected?.success===true,nativeRowCount:Array.isArray(selected?.rows)?selected.rows.length:-1};
      if (!current()) return {success:false,error:"Account changed during SNS details read"};
      if (!selected || selected.success!==true || !Array.isArray(selected.rows) || selected.rows.length!==parameters.postIds.length ||
          Buffer.byteLength(JSON.stringify(selected.rows),"utf8")>8*1024*1024) throw new Error("Native SNS details unavailable");
      const byTid=new Map();
      stage="rows";
      for (const row of selected.rows) {
        diagnostics.fieldTypes={tid:typeof row?.tid,user_name:typeof row?.user_name,content:typeof row?.content};
        if (!row || typeof row.tid!=="string" || !tids.includes(row.tid) || byTid.has(row.tid) ||
            typeof row.content!=="string" || !validIdentity(row.user_name)) throw new Error("Invalid native SNS details");
        byTid.set(row.tid,row);
      }
      const timeline=parameters.postIds.map((id,index)=>{
        const original=byTid.get(tids[index]);
        if (!original) throw new Error("SNS database identity mismatch");
        const row={tid:tids[index],id,username:original.user_name};nativeTid(row);
        stage="xml";
        diagnostics.xmlIndicators={startsWithMarkup:original.content.trimStart().startsWith("<"),hasTimelineTag:/<TimelineObject(?:\s|>)/.test(original.content),hasLocalExtraTag:/<LocalExtraInfo(?:\s|>)/.test(original.content),hasNul:original.content.includes("\0")};
        try {return parseSnsContent(original.content,row);} catch(error) {if(error.snsXmlShape)diagnostics.xmlShape=error.snsXmlShape;throw error;}
      });
      if (!current()) return {success:false,error:"Account changed during SNS details read"};
      return {success:true,timeline};
    } catch (error) {return {success:false,error:"Native SNS verified interactions unavailable",diagnostics:{stage,...diagnostics}};}
  }
  chatService.getSnsIdentityDetails632 = function(parameters) {
    const key=JSON.stringify(["details",chatService.getCacheAccountScope(),chatService.accountCacheEpoch,
      parameters.postIds]);
    return singleSnsRead(key,()=>readSnsDetails(parameters));
  };
  httpService.handleSnsIdentityDetails632 = async function(req,url,res) {
    if (req.method!=="GET") return this.sendMethodNotAllowed(res,"GET");
    let parameters;
    try {parameters=detailsParameters(url);} catch(error) {return this.sendError(res,400,String(error.message));}
    const result=await chatService.getSnsIdentityDetails632(parameters);
    if (result.success!==true) {
      // Recorded hM.sendJson always sends 200; errors need the real response API.
      res.setHeader("Content-Type","application/json; charset=utf-8");res.setHeader("Cache-Control","no-store");
      res.writeHead(503);res.end(JSON.stringify({success:false,error:result.error || "Native SNS verified interactions unavailable",diagnostics:result.diagnostics}));return;
    }
    res.setHeader("Cache-Control","no-store");res.setHeader("X-WeFlow-SNS-Identity-Details","1");
    this.sendJson(res,{success:true,version:1,capabilities:{snsIdentityDetails:1},count:result.timeline.length,timeline:result.timeline});
  };
  const originalBuildPayload = messagePushService.buildPayload;
  messagePushService.buildPayload = async function(session,message) {
    const payload = await originalBuildPayload.call(this,session,message);
    if (!payload || payload.event !== "message.new") return payload;
    return {...payload,senderUsername:message.senderUsername,isSend:message.isSend,
      localType:message.localType,localId:message.localId};
  };
}
