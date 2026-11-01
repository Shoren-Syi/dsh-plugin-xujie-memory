import assert from 'node:assert/strict'
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import {spawn} from 'node:child_process'
import {once} from 'node:events'
import {apply} from '../lib/index.js'
import {TYPERT_REMOTE} from '../lib/typert.remote-client.js'
const dir=mkdtempSync(join(tmpdir(),'xujie-node-'))
const token='isolated-test-token'
writeFileSync(join(dir,'token'),token,{mode:0o600})
const python=process.env.PYTHON ?? 'python3'
const kernel=spawn(python,['-u','-c',`from xujie_memory.server import serve\nfrom pathlib import Path\nh=serve(Path(${JSON.stringify(join(dir,'memory.db'))}),Path(${JSON.stringify(join(process.cwd(),'python/specs'))}),port=0,token=${JSON.stringify(token)})\nprint(h.server_port,flush=True)\nh.serve_forever()`],{env:{...process.env,PYTHONPATH:join(process.cwd(),'python')},stdio:['ignore','pipe','pipe']})
let checks=0
const check=(label,fn)=>{fn();checks++;console.log('PASS',label)}
const cleanups=[]
try {
  let text='';const port=await new Promise((resolve,reject)=>{const timeout=setTimeout(()=>reject(new Error('kernel startup timeout')),10000);kernel.stdout.on('data',chunk=>{text+=chunk;if(text.includes('\n')){clearTimeout(timeout);resolve(Number(text.trim().split('\n')[0]))}});kernel.once('error',reject);kernel.once('exit',code=>{if(!text)reject(new Error('kernel early exit '+code))})})
  const endpoint=`http://127.0.0.1:${port}`
  const handlers=new Map(),tools=new Map();let section
  const ctx={_services:{},invocation:{peer:{principal:{kind:'owner'}}},logger:{info(){},warn(){},debug(){}},tools:{register(t){tools.set(t.name,t);return()=>tools.delete(t.name)}},effect(fn){const dispose=fn();if(typeof dispose==='function')cleanups.push(dispose);return dispose},inject(deps,fn){fn(ctx)},get(key){if(key==='systemPrompt')return{section(s){section=s}};return ctx._services[key]},on(key,fn){handlers.set(key,fn);return()=>handlers.delete(key)}}
  apply(ctx,{autoStart:false,endpoint,tokenFile:join(dir,'token')})
  check('four memory tools are registered',()=>assert.deepEqual([...tools.keys()].sort(),['memory_edit','memory_forget','memory_search','memory_status']))
  const alice={id:'alice-one',userId:'alice'},bob={id:'bob-one',userId:'bob'}
  const event=text=>({type:'user/message',data:{source:{kind:'user'},content:[{type:'text',text}]}})
  handlers.get('session/event')(alice,event('我不吃辣'))
  const stats=await tools.get('memory_status').execute({}, {session:alice})
  check('automatic turn writes through real Python HTTP kernel',()=>assert.equal(stats.traces,1))
  check('prompt contains stored memory',()=>assert.ok(section.text({agent:{session:alice}}).includes('辣')))
  check('new user cannot inherit another user fallback',()=>assert.equal(section.text({agent:{session:bob}}),''))
  check('assembly without session does not expose fallback',()=>assert.equal(section.text({}),''))
  const controller=ctx._services.xujieMemory
  const catalog=await controller.catalog(),scope=catalog.scopes[0].scope_id
  const snapshot=await controller.snapshot(scope)
  check('generated snapshot codec accepts real projected DTO',()=>assert.ok(TYPERT_REMOTE.methods['xujieMemory/snapshot'].result.create().safeParse(snapshot).success))
  const node=snapshot.nodes.find(n=>n.kind==='trace')
  const saved=await controller.edit(scope,node.trace_id,'我喜欢咖啡',node.content_hash)
  check('generated edit codec accepts actual result',()=>assert.ok(TYPERT_REMOTE.methods['xujieMemory/edit'].result.create().safeParse(saved).success))
  check('UI mutation invalidates prompt cache',()=>assert.equal(section.text({agent:{session:alice}}),''))
  const hits=await controller.search(scope,'咖啡')
  check('search returns persisted replacement evidence',()=>assert.equal(hits[0].trace_id,saved.trace.trace_id))
  check('generated search codec accepts actual result',()=>assert.ok(TYPERT_REMOTE.methods['xujieMemory/search'].result.create().safeParse(hits).success))
  ctx.invocation={peer:{principal:{kind:'guest'}}}
  await assert.rejects(()=>controller.catalog(),e=>e.code==='xujie-memory/owner-required');checks++;console.log('PASS guest owner-only boundary')
  ctx.invocation={peer:{principal:{kind:'owner'}}}
  const modelEdit=await tools.get('memory_edit').execute({trace_id:hits[0].trace_id,content:'我喜欢绿茶',expected_hash:hits[0].content_hash},{session:alice})
  check('model correction tool writes the actual memory DB',()=>assert.equal(modelEdit.trace.content,'我喜欢绿茶'))
  await controller.forget(scope,modelEdit.trace.trace_id,modelEdit.trace.content_hash)
  const remaining=await controller.search(scope,'绿茶')
  check('GUI forget removes raw text from live DB search',()=>assert.deepEqual(remaining,[]))
} catch(error){console.error(error);process.exitCode=1}
finally{for(const dispose of cleanups.reverse())try{dispose()}catch{};kernel.kill();await Promise.race([once(kernel,'exit'),new Promise(r=>setTimeout(r,2000))]);rmSync(dir,{recursive:true,force:true});console.log('Completed checks:',checks)}
