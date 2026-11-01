import {readdirSync,rmSync,statSync,existsSync,readFileSync} from 'node:fs'
import {resolve,join} from 'node:path'
const root=resolve(import.meta.dirname,'..')
function clean(dir){for(const entry of readdirSync(dir,{withFileTypes:true})){const p=join(dir,entry.name);if(entry.isDirectory()){if(entry.name==='__pycache__')rmSync(p,{recursive:true,force:true});else clean(p)}else if(entry.name.endsWith('.pyc'))rmSync(p)}}
clean(join(root,'python'))
for(const name of ['lib/index.js','lib/controller.js','lib/client.js','lib/typert.host.js','lib/typert.remote-client.js','python/xujie_memory/management.py','ACCEPTANCE.md'])if(!existsSync(join(root,name)))throw new Error('Missing release artifact: '+name)
const m=JSON.parse(readFileSync(join(root,'package.json'),'utf8'))
if(m.version!=='0.7.0'||m.engines.dsh!=='0.2.1-rc.5')throw new Error('Unreviewed package/host version')
console.log('Release artifacts verified; Python bytecode caches excluded.')
