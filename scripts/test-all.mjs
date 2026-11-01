import {spawnSync} from 'node:child_process'
import {resolve} from 'node:path'
const root=resolve(import.meta.dirname,'..')
const python=process.env.PYTHON ?? (process.platform==='win32'?resolve(root,'runtime/win32-x64/python.exe'):'python3')
for (const [cmd,args] of [[python,['test/management_regression.py']],[process.execPath,['scripts/test-node.mjs']]]) {
 const r=spawnSync(cmd,args,{cwd:root,stdio:'inherit',env:{...process.env,PYTHON:python}})
 if(r.error)throw r.error
 if(r.status!==0)process.exit(r.status??1)
}
