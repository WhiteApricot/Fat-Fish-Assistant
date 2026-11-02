const fs=require('fs'),vm=require('vm'),assert=require('assert');
const script=fs.readFileSync(require('path').join(__dirname,'../src/fat_fish_health/views/launcher/launcher.js'),'utf8');
async function run(mode){
 const data={token:'synthetic-session',user:'synthetic',md5_pwd_hint:mode==='upgrade',change_pwd_hint:mode==='upgrade',expires:Math.floor(Date.now()/1000)+(mode==='expired'?-10:60)};
 const saved=new Map(),actions=[];
 const context={location:{hash:'#ffa-login='+encodeURIComponent(JSON.stringify(data)),hostname:'127.0.0.1',pathname:'/api/v1/plugins/fat_fish_health/views/launcher/_t/scoped/',replace:p=>actions.push(p)},history:{replaceState:()=>actions.push('fragment-cleared')},localStorage:{setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)},document:{getElementById:()=>({}),createElement:()=>({}),body:{appendChild:()=>{}}},fetch:async(url,options)=>{assert.strictEqual(actions[0],'fragment-cleared');assert.strictEqual(url,'/api/platform/stats');assert.strictEqual(options.headers.Authorization,'Bearer synthetic-session');return {ok:mode!=='invalid',json:async()=>({status:'ok'})}},Date,JSON,decodeURIComponent,Error};
 await vm.runInNewContext(script,context);
 if(mode==='ok'||mode==='upgrade'){assert.strictEqual(saved.get('token'),'synthetic-session');assert.strictEqual(actions.at(-1),mode==='upgrade'?'/#/auth/setup':'/#/dashboard/default');if(mode==='upgrade')assert.strictEqual(saved.get('md5_pwd_hint'),'true')}
 else assert.strictEqual(saved.size,0);
}
(async()=>{for(const mode of ['ok','expired','invalid','upgrade'])await run(mode);console.log('DASHBOARD_BROWSER_LOGIC_PASS')})().catch(e=>{console.error(e);process.exit(1)});
