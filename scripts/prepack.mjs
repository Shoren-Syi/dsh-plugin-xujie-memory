import {readdirSync,rmSync,statSync,existsSync,readFileSync} from 'node:fs'
import {resolve,join} from 'node:path'
const root=resolve(import.meta.dirname,'..')
function clean(dir){for(const entry of readdirSync(dir,{withFileTypes:true})){const p=join(dir,entry.name);if(entry.isDirectory()){if(entry.name==='__pycache__')rmSync(p,{recursive:true,force:true});else clean(p)}else if(entry.name.endsWith('.pyc'))rmSync(p);else if(/(?:\.db(?:-wal|-shm)?|\.sqlite(?:3)?|\.bak|\.log|\.tgz)$/i.test(entry.name)||entry.name==='token'||entry.name==='.env')throw new Error('运行目录包含不应发布的数据文件: '+p)}}
for(const dir of ['python','runtime','lib','src','scripts','test','vendor'])clean(join(root,dir))
for(const name of ['lib/index.js','lib/controller.js','lib/client.js','lib/typert.host.js','lib/typert.remote-client.js','python/xujie_memory/management.py','ACCEPTANCE.md'])if(!existsSync(join(root,name)))throw new Error('Missing release artifact: '+name)
const m=JSON.parse(readFileSync(join(root,'package.json'),'utf8'))
if(m.version!=='0.8.0-alpha.9'||m.engines.dsh!=='0.2.1-rc.5')throw new Error('Unreviewed package/host version')
console.log('Release artifacts verified; Python bytecode caches excluded.')
