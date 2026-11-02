var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __knownSymbol = (name, symbol) => (symbol = Symbol[name]) ? symbol : Symbol.for("Symbol." + name);
var __typeError = (msg) => {
  throw TypeError(msg);
};
var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });
var __decoratorStart = (base) => [, , , __create(base?.[__knownSymbol("metadata")] ?? null)];
var __decoratorStrings = ["class", "method", "getter", "setter", "accessor", "field", "value", "get", "set"];
var __expectFn = (fn) => fn !== void 0 && typeof fn !== "function" ? __typeError("Function expected") : fn;
var __decoratorContext = (kind, name, done, metadata, fns) => ({ kind: __decoratorStrings[kind], name, metadata, addInitializer: (fn) => done._ ? __typeError("Already initialized") : fns.push(__expectFn(fn || null)) });
var __decoratorMetadata = (array, target) => __defNormalProp(target, __knownSymbol("metadata"), array[3]);
var __runInitializers = (array, flags, self, value) => {
  for (var i = 0, fns = array[flags >> 1], n = fns && fns.length; i < n; i++) flags & 1 ? fns[i].call(self) : value = fns[i].call(self, value);
  return value;
};
var __decorateElement = (array, flags, name, decorators, target, extra) => {
  var fn, it, done, ctx, access, k = flags & 7, s = !!(flags & 8), p = !!(flags & 16);
  var j = k > 3 ? array.length + 1 : k ? s ? 1 : 2 : 0, key = __decoratorStrings[k + 5];
  var initializers = k > 3 && (array[j - 1] = []), extraInitializers = array[j] || (array[j] = []);
  var desc = k && (!p && !s && (target = target.prototype), k < 5 && (k > 3 || !p) && __getOwnPropDesc(k < 4 ? target : { get [name]() {
    return __privateGet(this, extra);
  }, set [name](x) {
    return __privateSet(this, extra, x);
  } }, name));
  k ? p && k < 4 && __name(extra, (k > 2 ? "set " : k > 1 ? "get " : "") + name) : __name(target, name);
  for (var i = decorators.length - 1; i >= 0; i--) {
    ctx = __decoratorContext(k, name, done = {}, array[3], extraInitializers);
    if (k) {
      ctx.static = s, ctx.private = p, access = ctx.access = { has: p ? (x) => __privateIn(target, x) : (x) => name in x };
      if (k ^ 3) access.get = p ? (x) => (k ^ 1 ? __privateGet : __privateMethod)(x, target, k ^ 4 ? extra : desc.get) : (x) => x[name];
      if (k > 2) access.set = p ? (x, y) => __privateSet(x, target, y, k ^ 4 ? extra : desc.set) : (x, y) => x[name] = y;
    }
    it = (0, decorators[i])(k ? k < 4 ? p ? extra : desc[key] : k > 4 ? void 0 : { get: desc.get, set: desc.set } : target, ctx), done._ = 1;
    if (k ^ 4 || it === void 0) __expectFn(it) && (k > 4 ? initializers.unshift(it) : k ? p ? extra = it : desc[key] = it : target = it);
    else if (typeof it !== "object" || it === null) __typeError("Object expected");
    else __expectFn(fn = it.get) && (desc.get = fn), __expectFn(fn = it.set) && (desc.set = fn), __expectFn(fn = it.init) && initializers.unshift(fn);
  }
  return k || __decoratorMetadata(array, target), desc && __defProp(target, name, desc), p ? k ^ 4 ? extra : desc : target;
};
var __accessCheck = (obj, member, msg) => member.has(obj) || __typeError("Cannot " + msg);
var __privateIn = (member, obj) => Object(obj) !== obj ? __typeError('Cannot use the "in" operator on this value') : member.has(obj);
var __privateGet = (obj, member, getter) => (__accessCheck(obj, member, "read from private field"), getter ? getter.call(obj) : member.get(obj));
var __privateSet = (obj, member, value, setter) => (__accessCheck(obj, member, "write to private field"), setter ? setter.call(obj, value) : member.set(obj, value), value);
var __privateMethod = (obj, member, method) => (__accessCheck(obj, member, "access private method"), method);

// vendor/dsh-protocol/index.ts
import { Context, Service } from "@deepseek-ai/cordis";

// vendor/dsh-protocol/remote-error.ts
var RemoteError = class extends Error {
  /**
   * @param code - stable failure code declared in {@link RemoteErrorDetailsMap}.
   * @param message - human diagnostic carried across the wire.
   * @param details - structured payload typed by the code.
   * @param options - standard Error options (`cause` survives in-process only).
   */
  constructor(code, message, details, options) {
    super(message, options);
    this.code = code;
    this.details = details;
    this.name = "RemoteError";
  }
  /** Structural marker: cross-realm/bundle identification never uses instanceof. */
  isDSHRemoteError = true;
};

// vendor/dsh-protocol/owned-value.ts
var TYPERT_OWNED_VALUE = Symbol.for("dsh.typert.owned-value");

// vendor/dsh-protocol/index.ts
var TYPERT_REMOTE_SEGMENT_PATTERN = /^[A-Za-z0-9_$.-]+$/;
function isTypertRemoteSegment(value) {
  return value !== "." && value !== ".." && TYPERT_REMOTE_SEGMENT_PATTERN.test(value);
}
var REMOTE_METHOD_DESCRIPTOR = "@deepseek-ai/dsh-typert-protocol/remote-methods";
function bindTypertRemote(service, serviceKey, options = {}) {
  validateName("service key", serviceKey);
  const namespace = options.namespace ?? serviceKey;
  validateName("namespace", namespace);
  const ctx = Reflect.get(service, "ctx");
  if (Context.is(ctx)) provideInvocationAccessor(ctx);
  return Object.freeze({ service, serviceKey, namespace });
}
var TypertRemoteService = class extends Service {
  /** Visible binding consumed by the Gateway's source-mode discovery. */
  typertRemote;
  /**
   * Register the Service and bind the same key to Typert Gateway.
   * @param ctx - owning Cordis Context.
   * @param serviceKey - exact Cordis service key and default wire namespace.
   * @param options - optional wire namespace.
   */
  constructor(ctx, serviceKey, options = {}) {
    super(ctx, serviceKey);
    this.typertRemote = bindTypertRemote(this, this.name, options);
  }
};
function provideInvocationAccessor(ctx) {
  if (Object.hasOwn(ctx.root.reflect.props, "invocation")) return;
  ctx.root.accessor("invocation", { get: () => void 0 });
}
function Remote(methodExportOrOptions, context) {
  if (typeof methodExportOrOptions === "string") {
    validateName("Remote export name", methodExportOrOptions);
    return remoteDecorator({ kind: "direct" }, { exportName: methodExportOrOptions });
  }
  if (typeof methodExportOrOptions === "object") {
    return remoteDecorator({ kind: "direct" }, markerOptions(
      methodExportOrOptions,
      REMOTE_OPTION_KEYS,
      'Remote options require name, mode: "stream", or resources'
    ));
  }
  if (context === void 0) throw new TypeError("typert-protocol: Remote decorator context is missing");
  addMarkerInitializer(context, { kind: "direct" });
}
var REMOTE_OPTION_KEYS = /* @__PURE__ */ new Set(["name", "mode", "resources"]);
function markerOptions(options, accepted, requirement) {
  const keys = Reflect.ownKeys(options);
  if (keys.length === 0 || keys.some((key) => typeof key !== "string" || !accepted.has(key)) || Object.hasOwn(options, "mode") && options.mode !== "stream") {
    throw new TypeError(`typert-protocol: ${requirement}`);
  }
  const { mode, name, resources } = options;
  if (name !== void 0) validateName("Remote export name", name);
  return {
    ...mode === void 0 ? {} : { mode },
    ...name === void 0 ? {} : { exportName: name },
    ...resources === void 0 ? {} : { resources: resourceReaders(resources) }
  };
}
function resourceReaders(resources) {
  const readers = {};
  for (const [kind, reader] of Object.entries(resources)) {
    if (typeof reader !== "function") {
      throw new TypeError(`typert-protocol: Remote resource reader "${kind}" must be a function`);
    }
    readers[kind] = reader;
  }
  if (Object.keys(readers).length === 0) throw new TypeError("typert-protocol: Remote resources require at least one reader");
  return Object.freeze(readers);
}
function remoteDecorator(invocation, options = {}) {
  return function(_method, context) {
    addMarkerInitializer(context, invocation, options);
  };
}
function readRemoteMethodDescriptor(prototype) {
  const property = Object.getOwnPropertyDescriptor(prototype, REMOTE_METHOD_DESCRIPTOR);
  if (property === void 0) return void 0;
  const descriptor = property.value;
  if (descriptor === null || typeof descriptor !== "object") {
    throw new TypeError("typert-protocol: Remote method descriptor must be an object");
  }
  const version = Reflect.get(descriptor, "version");
  if (version !== 1) {
    throw new TypeError(`typert-protocol: unsupported Remote method descriptor version ${String(version)}`);
  }
  const methods = Reflect.get(descriptor, "methods");
  if (!Array.isArray(methods)) {
    throw new TypeError("typert-protocol: Remote method descriptor methods must be an array");
  }
  return descriptor;
}
function addMarkerInitializer(context, invocation, options = {}) {
  if (context.private || context.static || typeof context.name !== "string") {
    throw new TypeError("typert-protocol: Remote decorators require a public instance method with a string name");
  }
  const method = context.name;
  context.addInitializer(function() {
    const prototype = Object.getPrototypeOf(this);
    if (prototype === null) {
      throw new TypeError(`typert-protocol: cannot mark Remote method "${method}" on an object without a prototype`);
    }
    mark(prototype, method, invocation, options);
  });
}
function mark(prototype, method, invocation, { mode, exportName, resources }) {
  const descriptor = readRemoteMethodDescriptor(prototype);
  const marker = Object.freeze({
    method,
    ...exportName === void 0 || exportName === method ? {} : { exportName },
    ...mode === void 0 ? {} : { mode },
    invocation: Object.freeze(invocation),
    ...resources === void 0 ? {} : { resources }
  });
  const current = descriptor?.methods.find((candidate) => candidate.method === method);
  if (current !== void 0) {
    if (current.exportName === marker.exportName && current.mode === marker.mode && current.resources === marker.resources && sameInvocation(current.invocation, invocation)) return;
    throw new Error(`typert-protocol: Remote method "${method}" has conflicting invocation markers`);
  }
  Object.defineProperty(prototype, REMOTE_METHOD_DESCRIPTOR, {
    configurable: true,
    value: Object.freeze({
      version: 1,
      methods: Object.freeze([...descriptor?.methods ?? [], marker])
    })
  });
}
function sameInvocation(left, right) {
  if (left.kind === "direct") return right.kind === "direct";
  if (right.kind === "direct") return false;
  return left.context === right.context;
}
function validateName(subject, value) {
  if (!isTypertRemoteSegment(value)) {
    throw new TypeError(`typert-protocol: ${subject} must contain only RPC endpoint segment characters`);
  }
}

// src/controller.ts
var _manage_dec, _forget_dec, _edit_dec, _search_dec, _detail_dec, _snapshot_dec, _catalog_dec, _a, _init;
var MemoryController = class extends (_a = TypertRemoteService, _catalog_dec = [Remote], _snapshot_dec = [Remote], _detail_dec = [Remote], _search_dec = [Remote], _edit_dec = [Remote], _forget_dec = [Remote], _manage_dec = [Remote], _a) {
  constructor(ctx, bridge) {
    super(ctx, "xujieMemory");
    this.bridge = bridge;
    __runInitializers(_init, 5, this);
  }
  owner() {
    if (this.ctx.invocation?.peer.principal.kind !== "owner") {
      throw new RemoteError("xujie-memory/owner-required", "\u53EA\u6709 DSH \u6240\u6709\u8005\u53EF\u4EE5\u7BA1\u7406\u8BB0\u5FC6\u5E93", { action: "memory/manage" });
    }
  }
  async call(path, body) {
    this.owner();
    try {
      return await this.bridge.request(path, body);
    } catch (error) {
      throw new RemoteError("xujie-memory/rejected", "\u8BB0\u5FC6\u5E93\u64CD\u4F5C\u672A\u5B8C\u6210", {
        reason: error instanceof Error ? error.message : "kernel_unavailable"
      });
    }
  }
  async catalog() {
    const r = await this.call("/manage/catalog", {});
    return { scope_mode: r.scope_mode, scopes: r.scopes };
  }
  async snapshot(scopeId) {
    const r = await this.call("/manage/snapshot", { scope_id: scopeId });
    const cards = r.cards.map((c) => ({
      account_id: c.profile.account_id,
      display_name: c.profile.display_name ?? c.profile.account_id,
      status: c.profile.status,
      tags: c.tags.map((t) => ({
        tag_id: t.tag_id,
        kind: t.kind,
        value: t.value,
        source: t.source,
        confidence: t.confidence
      }))
    }));
    const { scope_id, scope_mode, nodes, edges, emotions, truncated, limit } = r;
    const { traces, traces_erased, events, states, anchors, clarifications_open, summaries } = r.stats;
    return {
      scope_id,
      scope_mode,
      nodes,
      edges,
      cards,
      emotions,
      truncated,
      limit,
      stats: { traces, traces_erased, events, states, anchors, clarifications_open, summaries }
    };
  }
  async detail(scopeId, traceId) {
    return (await this.call("/manage/detail", { scope_id: scopeId, trace_id: traceId })).trace;
  }
  async search(scopeId, query) {
    const r = await this.call("/manage/search", { scope_id: scopeId, query });
    return r.hits.map((h) => ({
      trace_id: h.trace_id,
      content: h.content,
      content_hash: h.content_hash,
      score: h.score,
      priority: Number(h.priority ?? 0),
      importance: Number(h.importance ?? 1),
      anchor: Boolean(h.anchor),
      recorded_at: h.recorded_at ?? "",
      layer: h.layer
    }));
  }
  async edit(scopeId, traceId, content, expectedHash) {
    const r = await this.call("/manage/edit", { scope_id: scopeId, trace_id: traceId, content, expected_hash: expectedHash });
    return {
      changed: r.changed,
      trace: r.trace,
      ...r.replaced_trace_id ? { replaced_trace_id: r.replaced_trace_id, pending: r.pending } : {}
    };
  }
  async forget(scopeId, traceId, expectedHash) {
    const r = await this.call("/manage/forget", { scope_id: scopeId, trace_id: traceId, expected_hash: expectedHash });
    return { trace_id: r.trace_id, erased: r.erased, already_erased: r.already_erased, state_keys: r.state_keys };
  }
  async manage(scopeId, action, accountId, targetId, value, kind, expected) {
    await this.call("/manage/action", {
      scope_id: scopeId,
      action,
      account_id: accountId,
      target_id: targetId,
      value,
      kind,
      expected
    });
    return true;
  }
};
_init = __decoratorStart(_a);
__decorateElement(_init, 1, "catalog", _catalog_dec, MemoryController);
__decorateElement(_init, 1, "snapshot", _snapshot_dec, MemoryController);
__decorateElement(_init, 1, "detail", _detail_dec, MemoryController);
__decorateElement(_init, 1, "search", _search_dec, MemoryController);
__decorateElement(_init, 1, "edit", _edit_dec, MemoryController);
__decorateElement(_init, 1, "forget", _forget_dec, MemoryController);
__decorateElement(_init, 1, "manage", _manage_dec, MemoryController);
__decoratorMetadata(_init, MemoryController);
export {
  MemoryController
};
