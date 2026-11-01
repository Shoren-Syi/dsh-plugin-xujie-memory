// Test double only. These unit tests do not claim to launch the DSH desktop.
export class Context { static is() { return false } }
export class Service {
  constructor(ctx,name) {this.ctx=ctx;this.name=name;ctx._services ??= {};ctx._services[name]=this}
}
