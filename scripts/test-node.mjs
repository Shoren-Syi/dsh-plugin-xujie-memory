import {build} from 'esbuild'
import {spawnSync} from 'node:child_process'
import {mkdtempSync,rmSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join,resolve} from 'node:path'
const root=resolve(import.meta.dirname,'..'),dir=mkdtempSync(join(tmpdir(),'xujie-test-build-')),outfile=join(dir,'test.mjs')
try{
await build({entryPoints:[join(root,'test/node-regression.entry.mjs')],bundle:true,platform:'node',format:'esm',target:'node22',outfile,external:['node:*'],alias:{'@deepseek-ai/cordis':join(root,'test/cordis-fixture.mjs')},logLevel:'warning'})
const r=spawnSync(process.execPath,[outfile],{cwd:root,stdio:'inherit',env:process.env});process.exitCode=r.status??1
}finally{rmSync(dir,{recursive:true,force:true})}
