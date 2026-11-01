import { build } from 'esbuild'
import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
const root = resolve(import.meta.dirname, '..')
const alias = {
  '@deepseek-ai/dsh-typert-protocol/remote-info': resolve(root, 'vendor/dsh-protocol/remote-info.ts'),
  '@deepseek-ai/dsh-typert-protocol': resolve(root, 'vendor/dsh-protocol/index.ts'),
}
const common = { bundle:true,format:'esm',target:'es2022',logLevel:'warning',alias }
await build({...common,entryPoints:[resolve(root,'src/controller.ts')],platform:'node',external:['@deepseek-ai/cordis'],outfile:resolve(root,'lib/controller.js')})
for (const name of ['typert.host','typert.remote-client']) {
  await build({...common,entryPoints:[resolve(root,`src/generated/${name}.js`)],platform:'neutral',outfile:resolve(root,`lib/${name}.js`)})
}
writeFileSync(resolve(root,'lib/types.d.ts'),readFileSync(resolve(root,'src/types.ts')))
writeFileSync(resolve(root,'lib/types.js'),'export {};\n')
if (process.argv.includes('--host-only')) process.exit(0)
const manifest = JSON.parse(readFileSync(resolve(root,'package.json'),'utf8'))
await build({...common,entryPoints:[resolve(root,'src/client/index.tsx')],platform:'browser',format:'cjs',external:['react','react/jsx-runtime','react-dom','react-dom/client','@deepseek-ai/cordis'],jsx:'automatic',outfile:resolve(root,'lib/client.js'),loader:{'.css':'text'},banner:{js:`window.__ModuleLoader__.load({id:${JSON.stringify(manifest.name)},factory:(require)=>{var module={exports:{}};var exports=module.exports;`},footer:{js:'return module.exports;}});'}})
console.log('Built host controller, strict contracts and DSH client factory.')
