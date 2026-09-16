export function instantiate(getCoreModule, imports, instantiateCore = (module, importObject) => new WebAssembly.Instance(module, importObject)) {
  
  let dv = new DataView(new ArrayBuffer());
  const dataView = mem => dv.buffer === mem.buffer ? dv : dv = new DataView(mem.buffer);
  
  const toInt64 = val => BigInt.asIntN(64, BigInt(val));
  
  const toUint64 = val => BigInt.asUintN(64, BigInt(val));
  
  function toInt32(val) {
    return val >> 0;
  }
  
  function toUint16(val) {
    val >>>= 0;
    val %= 2 ** 16;
    return val;
  }
  
  function toUint32(val) {
    return val >>> 0;
  }
  
  function toUint8(val) {
    val >>>= 0;
    val %= 2 ** 8;
    return val;
  }
  
  const utf8Decoder = new TextDecoder();
  
  const utf8Encoder = new TextEncoder();
  let utf8EncodedLen = 0;
  function utf8Encode(s, realloc, memory) {
    if (typeof s !== 'string') throw new TypeError('expected a string');
    if (s.length === 0) {
      utf8EncodedLen = 0;
      return 1;
    }
    let buf = utf8Encoder.encode(s);
    let ptr = realloc(0, 0, 1, buf.length);
    new Uint8Array(memory.buffer).set(buf, ptr);
    utf8EncodedLen = buf.length;
    return ptr;
  }
  
  let NEXT_TASK_ID = 0n;
  function startCurrentTask(componentIdx, isAsync, entryFnName) {
    _debugLog('[startCurrentTask()] args', { componentIdx, isAsync });
    if (componentIdx === undefined || componentIdx === null) {
      throw new Error('missing/invalid component instance index while starting task');
    }
    const tasks = ASYNC_TASKS_BY_COMPONENT_IDX.get(componentIdx);
    
    const nextId = ++NEXT_TASK_ID;
    const newTask = new AsyncTask({ id: nextId, componentIdx, isAsync, entryFnName });
    const newTaskMeta = { id: nextId, componentIdx, task: newTask };
    
    ASYNC_CURRENT_TASK_IDS.push(nextId);
    ASYNC_CURRENT_COMPONENT_IDXS.push(componentIdx);
    
    if (!tasks) {
      ASYNC_TASKS_BY_COMPONENT_IDX.set(componentIdx, [newTaskMeta]);
      return nextId;
    } else {
      tasks.push(newTaskMeta);
    }
    
    return nextId;
  }
  
  function endCurrentTask(componentIdx, taskId) {
    _debugLog('[endCurrentTask()] args', { componentIdx });
    componentIdx ??= ASYNC_CURRENT_COMPONENT_IDXS.at(-1);
    taskId ??= ASYNC_CURRENT_TASK_IDS.at(-1);
    if (componentIdx === undefined || componentIdx === null) {
      throw new Error('missing/invalid component instance index while ending current task');
    }
    const tasks = ASYNC_TASKS_BY_COMPONENT_IDX.get(componentIdx);
    if (!tasks || !Array.isArray(tasks)) {
      throw new Error('missing/invalid tasks for component instance while ending task');
    }
    if (tasks.length == 0) {
      throw new Error('no current task(s) for component instance while ending task');
    }
    
    if (taskId) {
      const last = tasks[tasks.length - 1];
      if (last.id !== taskId) {
        throw new Error('current task does not match expected task ID');
      }
    }
    
    ASYNC_CURRENT_TASK_IDS.pop();
    ASYNC_CURRENT_COMPONENT_IDXS.pop();
    
    return tasks.pop();
  }
  const ASYNC_TASKS_BY_COMPONENT_IDX = new Map();
  const ASYNC_CURRENT_TASK_IDS = [];
  const ASYNC_CURRENT_COMPONENT_IDXS = [];
  
  class AsyncTask {
    static State = {
      INITIAL: 'initial',
      CANCELLED: 'cancelled',
      CANCEL_PENDING: 'cancel-pending',
      CANCEL_DELIVERED: 'cancel-delivered',
      RESOLVED: 'resolved',
    }
    
    static BlockResult = {
      CANCELLED: 'block.cancelled',
      NOT_CANCELLED: 'block.not-cancelled',
    }
    
    #id;
    #componentIdx;
    #state;
    #isAsync;
    #onResolve = null;
    #entryFnName = null;
    #subtasks = [];
    #completionPromise = null;
    
    cancelled = false;
    requested = false;
    alwaysTaskReturn = false;
    
    returnCalls =  0;
    storage = [0, 0];
    borrowedHandles = {};
    
    awaitableResume = null;
    awaitableCancel = null;
    
    
    constructor(opts) {
      if (opts?.id === undefined) { throw new TypeError('missing task ID during task creation'); }
      this.#id = opts.id;
      if (opts?.componentIdx === undefined) {
        throw new TypeError('missing component id during task creation');
      }
      this.#componentIdx = opts.componentIdx;
      this.#state = AsyncTask.State.INITIAL;
      this.#isAsync = opts?.isAsync ?? false;
      this.#entryFnName = opts.entryFnName;
      
      const {
        promise: completionPromise,
        resolve: resolveCompletionPromise,
        reject: rejectCompletionPromise,
      } = Promise.withResolvers();
      this.#completionPromise = completionPromise;
      
      this.#onResolve = (results) => {
        // TODO: handle external facing cancellation (should likely be a rejection)
        resolveCompletionPromise(results);
      }
    }
    
    taskState() { return this.#state.slice(); }
    id() { return this.#id; }
    componentIdx() { return this.#componentIdx; }
    isAsync() { return this.#isAsync; }
    entryFnName() { return this.#entryFnName; }
    completionPromise() { return this.#completionPromise; }
    
    mayEnter(task) {
      const cstate = getOrCreateAsyncState(this.#componentIdx);
      if (!cstate.backpressure) {
        _debugLog('[AsyncTask#mayEnter()] disallowed due to backpressure', { taskID: this.#id });
        return false;
      }
      if (!cstate.callingSyncImport()) {
        _debugLog('[AsyncTask#mayEnter()] disallowed due to sync import call', { taskID: this.#id });
        return false;
      }
      const callingSyncExportWithSyncPending = cstate.callingSyncExport && !task.isAsync;
      if (!callingSyncExportWithSyncPending) {
        _debugLog('[AsyncTask#mayEnter()] disallowed due to sync export w/ sync pending', { taskID: this.#id });
        return false;
      }
      return true;
    }
    
    async enter() {
      _debugLog('[AsyncTask#enter()] args', { taskID: this.#id });
      
      // TODO: assert scheduler locked
      // TODO: trap if on the stack
      
      const cstate = getOrCreateAsyncState(this.#componentIdx);
      
      let mayNotEnter = !this.mayEnter(this);
      const componentHasPendingTasks = cstate.pendingTasks > 0;
      if (mayNotEnter || componentHasPendingTasks) {
        throw new Error('in enter()'); // TODO: remove
        cstate.pendingTasks.set(this.#id, new Awaitable(new Promise()));
        
        const blockResult = await this.onBlock(awaitable);
        if (blockResult) {
          // TODO: find this pending task in the component
          const pendingTask = cstate.pendingTasks.get(this.#id);
          if (!pendingTask) {
            throw new Error('pending task [' + this.#id + '] not found for component instance');
          }
          cstate.pendingTasks.remove(this.#id);
          this.#onResolve(new Error('failed enter'));
          return false;
        }
        
        mayNotEnter = !this.mayEnter(this);
        if (!mayNotEnter || !cstate.startPendingTask) {
          throw new Error('invalid component entrance/pending task resolution');
        }
        cstate.startPendingTask = false;
      }
      
      if (!this.isAsync) { cstate.callingSyncExport = true; }
      
      return true;
    }
    
    async waitForEvent(opts) {
      const { waitableSetRep, isAsync } = opts;
      _debugLog('[AsyncTask#waitForEvent()] args', { taskID: this.#id, waitableSetRep, isAsync });
      
      if (this.#isAsync !== isAsync) {
        throw new Error('async waitForEvent called on non-async task');
      }
      
      if (this.status === AsyncTask.State.CANCEL_PENDING) {
        this.#state = AsyncTask.State.CANCEL_DELIVERED;
        return {
          code: ASYNC_EVENT_CODE.TASK_CANCELLED,
        };
      }
      
      const state = getOrCreateAsyncState(this.#componentIdx);
      const waitableSet = state.waitableSets.get(waitableSetRep);
      if (!waitableSet) { throw new Error('missing/invalid waitable set'); }
      
      waitableSet.numWaiting += 1;
      let event = null;
      
      while (event == null) {
        const awaitable = new Awaitable(waitableSet.getPendingEvent());
        const waited = await this.blockOn({ awaitable, isAsync, isCancellable: true });
        if (waited) {
          if (this.#state !== AsyncTask.State.INITIAL) {
            throw new Error('task should be in initial state found [' + this.#state + ']');
          }
          this.#state = AsyncTask.State.CANCELLED;
          return {
            code: ASYNC_EVENT_CODE.TASK_CANCELLED,
          };
        }
        
        event = waitableSet.poll();
      }
      
      waitableSet.numWaiting -= 1;
      return event;
    }
    
    waitForEventSync(opts) {
      throw new Error('AsyncTask#yieldSync() not implemented')
    }
    
    async pollForEvent(opts) {
      const { waitableSetRep, isAsync } = opts;
      _debugLog('[AsyncTask#pollForEvent()] args', { taskID: this.#id, waitableSetRep, isAsync });
      
      if (this.#isAsync !== isAsync) {
        throw new Error('async pollForEvent called on non-async task');
      }
      
      throw new Error('AsyncTask#pollForEvent() not implemented');
    }
    
    pollForEventSync(opts) {
      throw new Error('AsyncTask#yieldSync() not implemented')
    }
    
    async blockOn(opts) {
      const { awaitable, isCancellable, forCallback } = opts;
      _debugLog('[AsyncTask#blockOn()] args', { taskID: this.#id, awaitable, isCancellable, forCallback });
      
      if (awaitable.resolved() && !ASYNC_DETERMINISM && _coinFlip()) {
        return AsyncTask.BlockResult.NOT_CANCELLED;
      }
      
      const cstate = getOrCreateAsyncState(this.#componentIdx);
      if (forCallback) { cstate.exclusiveRelease(); }
      
      let cancelled = await this.onBlock(awaitable);
      if (cancelled === AsyncTask.BlockResult.CANCELLED && !isCancellable) {
        const secondCancel = await this.onBlock(awaitable);
        if (secondCancel !== AsyncTask.BlockResult.NOT_CANCELLED) {
          throw new Error('uncancellable task was canceled despite second onBlock()');
        }
      }
      
      if (forCallback) {
        const acquired = new Awaitable(cstate.exclusiveLock());
        cancelled = await this.onBlock(acquired);
        if (cancelled === AsyncTask.BlockResult.CANCELLED) {
          const secondCancel = await this.onBlock(acquired);
          if (secondCancel !== AsyncTask.BlockResult.NOT_CANCELLED) {
            throw new Error('uncancellable callback task was canceled despite second onBlock()');
          }
        }
      }
      
      if (cancelled === AsyncTask.BlockResult.CANCELLED) {
        if (this.#state !== AsyncTask.State.INITIAL) {
          throw new Error('cancelled task is not at initial state');
        }
        if (isCancellable) {
          this.#state = AsyncTask.State.CANCELLED;
          return AsyncTask.BlockResult.CANCELLED;
        } else {
          this.#state = AsyncTask.State.CANCEL_PENDING;
          return AsyncTask.BlockResult.NOT_CANCELLED;
        }
      }
      
      return AsyncTask.BlockResult.NOT_CANCELLED;
    }
    
    async onBlock(awaitable) {
      _debugLog('[AsyncTask#onBlock()] args', { taskID: this.#id, awaitable });
      if (!(awaitable instanceof Awaitable)) {
        throw new Error('invalid awaitable during onBlock');
      }
      
      // Build a promise that this task can await on which resolves when it is awoken
      const { promise, resolve, reject } = Promise.withResolvers();
      this.awaitableResume = () => {
        _debugLog('[AsyncTask] resuming after onBlock', { taskID: this.#id });
        resolve();
      };
      this.awaitableCancel = (err) => {
        _debugLog('[AsyncTask] rejecting after onBlock', { taskID: this.#id, err });
        reject(err);
      };
      
      // Park this task/execution to be handled later
      const state = getOrCreateAsyncState(this.#componentIdx);
      state.parkTaskOnAwaitable({ awaitable, task: this });
      
      try {
        await promise;
        return AsyncTask.BlockResult.NOT_CANCELLED;
      } catch (err) {
        // rejection means task cancellation
        return AsyncTask.BlockResult.CANCELLED;
      }
    }
    
    async asyncOnBlock(awaitable) {
      _debugLog('[AsyncTask#asyncOnBlock()] args', { taskID: this.#id, awaitable });
      if (!(awaitable instanceof Awaitable)) {
        throw new Error('invalid awaitable during onBlock');
      }
      // TODO: watch for waitable AND cancellation
      // TODO: if it WAS cancelled:
      // - return true
      // - only once per subtask
      // - do not wait on the scheduler
      // - control flow should go to the subtask (only once)
      // - Once subtask blocks/resolves, reqlinquishControl() will tehn resolve request_cancel_end (without scheduler lock release)
      // - control flow goes back to request_cancel
      //
      // Subtask cancellation should work similarly to an async import call -- runs sync up until
      // the subtask blocks or resolves
      //
      throw new Error('AsyncTask#asyncOnBlock() not yet implemented');
    }
    
    async yield(opts) {
      const { isCancellable, forCallback } = opts;
      _debugLog('[AsyncTask#yield()] args', { taskID: this.#id, isCancellable, forCallback });
      
      if (isCancellable && this.status === AsyncTask.State.CANCEL_PENDING) {
        this.#state = AsyncTask.State.CANCELLED;
        return {
          code: ASYNC_EVENT_CODE.TASK_CANCELLED,
          payload: [0, 0],
        };
      }
      
      // TODO: Awaitables need to *always* trigger the parking mechanism when they're done...?
      // TODO: Component async state should remember which awaitables are done and work to clear tasks waiting
      
      const blockResult = await this.blockOn({
        awaitable: new Awaitable(new Promise(resolve => setTimeout(resolve, 0))),
        isCancellable,
        forCallback,
      });
      
      if (blockResult === AsyncTask.BlockResult.CANCELLED) {
        if (this.#state !== AsyncTask.State.INITIAL) {
          throw new Error('task should be in initial state found [' + this.#state + ']');
        }
        this.#state = AsyncTask.State.CANCELLED;
        return {
          code: ASYNC_EVENT_CODE.TASK_CANCELLED,
          payload: [0, 0],
        };
      }
      
      return {
        code: ASYNC_EVENT_CODE.NONE,
        payload: [0, 0],
      };
    }
    
    yieldSync(opts) {
      throw new Error('AsyncTask#yieldSync() not implemented')
    }
    
    cancel() {
      _debugLog('[AsyncTask#cancel()] args', { });
      if (!this.taskState() !== AsyncTask.State.CANCEL_DELIVERED) {
        throw new Error('invalid task state for cancellation');
      }
      if (this.borrowedHandles.length > 0) { throw new Error('task still has borrow handles'); }
      
      this.#onResolve(new Error('cancelled'));
      this.#state = AsyncTask.State.RESOLVED;
    }
    
    resolve(results) {
      _debugLog('[AsyncTask#resolve()] args', { results });
      if (this.#state === AsyncTask.State.RESOLVED) {
        throw new Error('task is already resolved');
      }
      if (this.borrowedHandles.length > 0) { throw new Error('task still has borrow handles'); }
      this.#onResolve(results.length === 1 ? results[0] : results);
      this.#state = AsyncTask.State.RESOLVED;
    }
    
    exit() {
      _debugLog('[AsyncTask#exit()] args', { });
      
      // TODO: ensure there is only one task at a time (scheduler.lock() functionality)
      if (this.#state !== AsyncTask.State.RESOLVED) {
        throw new Error('task exited without resolution');
      }
      if (this.borrowedHandles > 0) {
        throw new Error('task exited without clearing borrowed handles');
      }
      
      const state = getOrCreateAsyncState(this.#componentIdx);
      if (!state) { throw new Error('missing async state for component [' + this.#componentIdx + ']'); }
      if (!this.#isAsync && !state.inSyncExportCall) {
        throw new Error('sync task must be run from components known to be in a sync export call');
      }
      state.inSyncExportCall = false;
      
      this.startPendingTask();
    }
    
    startPendingTask(args) {
      _debugLog('[AsyncTask#startPendingTask()] args', args);
      throw new Error('AsyncTask#startPendingTask() not implemented');
    }
    
    createSubtask(args) {
      _debugLog('[AsyncTask#createSubtask()] args', args);
      const newSubtask = new AsyncSubtask({
        componentIdx: this.componentIdx(),
        taskID: this.id(),
        memoryIdx: args?.memoryIdx,
      });
      this.#subtasks.push(newSubtask);
      return newSubtask;
    }
    
    currentSubtask() {
      _debugLog('[AsyncTask#currentSubtask()]');
      if (this.#subtasks.length === 0) { throw new Error('no current subtask'); }
      return this.#subtasks.at(-1);
    }
    
    endCurrentSubtask() {
      _debugLog('[AsyncTask#endCurrentSubtask()]');
      if (this.#subtasks.length === 0) { throw new Error('cannot end current subtask: no current subtask'); }
      const subtask = this.#subtasks.pop();
      subtask.drop();
      return subtask;
    }
  }
  
  function unpackCallbackResult(result) {
    _debugLog('[unpackCallbackResult()] args', { result });
    if (!(_typeCheckValidI32(result))) { throw new Error('invalid callback return value [' + result + '], not a valid i32'); }
    const eventCode = result & 0xF;
    if (eventCode < 0 || eventCode > 3) {
      throw new Error('invalid async return value [' + eventCode + '], outside callback code range');
    }
    if (result < 0 || result >= 2**32) { throw new Error('invalid callback result'); }
    // TODO: table max length check?
    const waitableSetIdx = result >> 4;
    return [eventCode, waitableSetIdx];
  }
  const ASYNC_STATE = new Map();
  
  function getOrCreateAsyncState(componentIdx, init) {
    if (!ASYNC_STATE.has(componentIdx)) {
      ASYNC_STATE.set(componentIdx, new ComponentAsyncState());
    }
    return ASYNC_STATE.get(componentIdx);
  }
  
  class ComponentAsyncState {
    #callingAsyncImport = false;
    #syncImportWait = Promise.withResolvers();
    #lock = null;
    
    mayLeave = true;
    waitableSets = new RepTable();
    waitables = new RepTable();
    
    #parkedTasks = new Map();
    
    callingSyncImport(val) {
      if (val === undefined) { return this.#callingAsyncImport; }
      if (typeof val !== 'boolean') { throw new TypeError('invalid setting for async import'); }
      const prev = this.#callingAsyncImport;
      this.#callingAsyncImport = val;
      if (prev === true && this.#callingAsyncImport === false) {
        this.#notifySyncImportEnd();
      }
    }
    
    #notifySyncImportEnd() {
      const existing = this.#syncImportWait;
      this.#syncImportWait = Promise.withResolvers();
      existing.resolve();
    }
    
    async waitForSyncImportCallEnd() {
      await this.#syncImportWait.promise;
    }
    
    parkTaskOnAwaitable(args) {
      if (!args.awaitable) { throw new TypeError('missing awaitable when trying to park'); }
      if (!args.task) { throw new TypeError('missing task when trying to park'); }
      const { awaitable, task } = args;
      
      let taskList = this.#parkedTasks.get(awaitable.id());
      if (!taskList) {
        taskList = [];
        this.#parkedTasks.set(awaitable.id(), taskList);
      }
      taskList.push(task);
      
      this.wakeNextTaskForAwaitable(awaitable);
    }
    
    wakeNextTaskForAwaitable(awaitable) {
      if (!awaitable) { throw new TypeError('missing awaitable when waking next task'); }
      const awaitableID = awaitable.id();
      
      const taskList = this.#parkedTasks.get(awaitableID);
      if (!taskList || taskList.length === 0) {
        _debugLog('[ComponentAsyncState] no tasks waiting for awaitable', { awaitableID: awaitable.id() });
        return;
      }
      
      let task = taskList.shift(); // todo(perf)
      if (!task) { throw new Error('no task in parked list despite previous check'); }
      
      if (!task.awaitableResume) {
        throw new Error('task ready due to awaitable is missing resume', { taskID: task.id(), awaitableID });
      }
      task.awaitableResume();
    }
    
    async exclusiveLock() {  // TODO: use atomics
    if (this.#lock === null) {
      this.#lock = { ticket: 0n };
    }
    
    // Take a ticket for the next valid usage
    const ticket = ++this.#lock.ticket;
    
    _debugLog('[ComponentAsyncState#exclusiveLock()] locking', {
      currentTicket: ticket - 1n,
      ticket
    });
    
    // If there is an active promise, then wait for it
    let finishedTicket;
    while (this.#lock.promise) {
      finishedTicket = await this.#lock.promise;
      if (finishedTicket === ticket - 1n) { break; }
    }
    
    const { promise, resolve } = Promise.withResolvers();
    this.#lock = {
      ticket,
      promise,
      resolve,
    };
    
    return this.#lock.promise;
  }
  
  exclusiveRelease() {
    _debugLog('[ComponentAsyncState#exclusiveRelease()] releasing', {
      currentTicket: this.#lock === null ? 'none' : this.#lock.ticket,
    });
    
    if (this.#lock === null) { return; }
    
    const existingLock = this.#lock;
    this.#lock = null;
    existingLock.resolve(existingLock.ticket);
  }
  
  isExclusivelyLocked() { return this.#lock !== null; }
  
}

function prepareCall(memoryIdx) {
  _debugLog('[prepareCall()] args', { memoryIdx });
  
  const taskMeta = getCurrentTask(ASYNC_CURRENT_COMPONENT_IDXS.at(-1), ASYNC_CURRENT_TASK_IDS.at(-1));
  if (!taskMeta) { throw new Error('invalid/missing current async task meta during prepare call'); }
  
  const task = taskMeta.task;
  if (!task) { throw new Error('unexpectedly missing task in task meta during prepare call'); }
  
  const state = getOrCreateAsyncState(task.componentIdx());
  if (!state) {
    throw new Error('invalid/missing async state for component instance [' + componentInstanceID + ']');
  }
  
  const subtask = task.createSubtask({
    memoryIdx,
  });
  
}

function asyncStartCall(callbackIdx, postReturnIdx) {
  _debugLog('[asyncStartCall()] args', { callbackIdx, postReturnIdx });
  
  const taskMeta = getCurrentTask(ASYNC_CURRENT_COMPONENT_IDXS.at(-1), ASYNC_CURRENT_TASK_IDS.at(-1));
  if (!taskMeta) { throw new Error('invalid/missing current async task meta during prepare call'); }
  
  const task = taskMeta.task;
  if (!task) { throw new Error('unexpectedly missing task in task meta during prepare call'); }
  
  const subtask = task.currentSubtask();
  if (!subtask) { throw new Error('invalid/missing subtask during async start call'); }
  
  return Number(subtask.waitableRep()) << 4 | subtask.getStateNumber();
}

function syncStartCall(callbackIdx) {
  _debugLog('[syncStartCall()] args', { callbackIdx });
}

if (!Promise.withResolvers) {
  Promise.withResolvers = () => {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  };
}

const _debugLog = (...args) => {
  if (!globalThis?.process?.env?.JCO_DEBUG) { return; }
  console.debug(...args);
}
const ASYNC_DETERMINISM = 'random';
const _coinFlip = () => { return Math.random() > 0.5; };
const I32_MAX = 2_147_483_647;
const I32_MIN = -2_147_483_648;
const _typeCheckValidI32 = (n) => typeof n === 'number' && n >= I32_MIN && n <= I32_MAX;

function clampGuest(i, min, max) {
  if (i < min || i > max) throw new TypeError(`must be between ${min} and ${max}`);
  return i;
}

class ComponentError extends Error {
  constructor (value) {
    const enumerable = typeof value !== 'string';
    super(enumerable ? `${String(value)} (see error.payload)` : value);
    Object.defineProperty(this, 'payload', { value, enumerable });
  }
}

function getErrorPayload(e) {
  if (e && hasOwnProperty.call(e, 'payload')) return e.payload;
  if (e instanceof Error) throw e;
  return e;
}

class RepTable {
  #data = [0, null];
  
  insert(val) {
    _debugLog('[RepTable#insert()] args', { val });
    const freeIdx = this.#data[0];
    if (freeIdx === 0) {
      this.#data.push(val);
      this.#data.push(null);
      return (this.#data.length >> 1) - 1;
    }
    this.#data[0] = this.#data[freeIdx << 1];
    const placementIdx = freeIdx << 1;
    this.#data[placementIdx] = val;
    this.#data[placementIdx + 1] = null;
    return freeIdx;
  }
  
  get(rep) {
    _debugLog('[RepTable#get()] args', { rep });
    const baseIdx = rep << 1;
    const val = this.#data[baseIdx];
    return val;
  }
  
  contains(rep) {
    _debugLog('[RepTable#contains()] args', { rep });
    const baseIdx = rep << 1;
    return !!this.#data[baseIdx];
  }
  
  remove(rep) {
    _debugLog('[RepTable#remove()] args', { rep });
    if (this.#data.length === 2) { throw new Error('invalid'); }
    
    const baseIdx = rep << 1;
    const val = this.#data[baseIdx];
    if (val === 0) { throw new Error('invalid resource rep (cannot be 0)'); }
    
    this.#data[baseIdx] = this.#data[0];
    this.#data[0] = rep;
    
    return val;
  }
  
  clear() {
    _debugLog('[RepTable#clear()] args', { rep });
    this.#data = [0, null];
  }
}

function throwInvalidBool() {
  throw new TypeError('invalid variant discriminant for bool');
}

const hasOwnProperty = Object.prototype.hasOwnProperty;


const module0 = getCoreModule('app.core.wasm');
const module1 = getCoreModule('app.core2.wasm');
const module2 = getCoreModule('app.core3.wasm');

const { monotonicNow, wallNow } = imports['vibapp:experimental-v0/clock'];
const { describeHost } = imports['vibapp:experimental-v0/host-info'];
const { scanPrefix } = imports['vibapp:experimental-v0/kv'];
const { write } = imports['vibapp:experimental-v0/log'];
const { current } = imports['vibapp:experimental-v0/settings'];
let gen = (function* _initGenerator () {
  let exports0;
  
  function trampoline0() {
    _debugLog('[iface="vibapp:experimental-v0/clock@0.0.1", function="monotonic-now"] [Instruction::CallInterface] (async? sync, @ enter)');
    const _interface_call_currentTaskID = startCurrentTask(0, false, 'monotonic-now');
    const ret = monotonicNow();
    _debugLog('[iface="vibapp:experimental-v0/clock@0.0.1", function="monotonic-now"] [Instruction::CallInterface] (sync, @ post-call)');
    endCurrentTask(0);
    _debugLog('[iface="vibapp:experimental-v0/clock@0.0.1", function="monotonic-now"][Instruction::Return]', {
      funcName: 'monotonic-now',
      paramCount: 1,
      async: false,
      postReturn: false
    });
    return toUint64(ret);
  }
  
  let exports1;
  let memory0;
  let realloc0;
  
  function trampoline1(arg0) {
    _debugLog('[iface="vibapp:experimental-v0/clock@0.0.1", function="wall-now"] [Instruction::CallInterface] (async? sync, @ enter)');
    const _interface_call_currentTaskID = startCurrentTask(0, false, 'wall-now');
    let ret;
    try {
      ret = { tag: 'ok', val: wallNow()};
    } catch (e) {
      ret = { tag: 'err', val: getErrorPayload(e) };
    }
    _debugLog('[iface="vibapp:experimental-v0/clock@0.0.1", function="wall-now"] [Instruction::CallInterface] (sync, @ post-call)');
    endCurrentTask(0);
    var variant6 = ret;
    switch (variant6.tag) {
      case 'ok': {
        const e = variant6.val;
        dataView(memory0).setInt8(arg0 + 0, 0, true);
        var {nowUtc: v0_0, timeZone: v0_1, utcOffsetSeconds: v0_2 } = e;
        var ptr1 = utf8Encode(v0_0, realloc0, memory0);
        var len1 = utf8EncodedLen;
        dataView(memory0).setUint32(arg0 + 8, len1, true);
        dataView(memory0).setUint32(arg0 + 4, ptr1, true);
        var ptr2 = utf8Encode(v0_1, realloc0, memory0);
        var len2 = utf8EncodedLen;
        dataView(memory0).setUint32(arg0 + 16, len2, true);
        dataView(memory0).setUint32(arg0 + 12, ptr2, true);
        dataView(memory0).setInt32(arg0 + 20, toInt32(v0_2), true);
        break;
      }
      case 'err': {
        const e = variant6.val;
        dataView(memory0).setInt8(arg0 + 0, 1, true);
        var {code: v3_0, message: v3_1, retryable: v3_2 } = e;
        var val4 = v3_0;
        let enum4;
        switch (val4) {
          case 'invalid-argument': {
            enum4 = 0;
            break;
          }
          case 'incompatible-contract': {
            enum4 = 1;
            break;
          }
          case 'unsupported-version': {
            enum4 = 2;
            break;
          }
          case 'unknown-interface': {
            enum4 = 3;
            break;
          }
          case 'missing-interface': {
            enum4 = 4;
            break;
          }
          case 'permission-denied': {
            enum4 = 5;
            break;
          }
          case 'consent-required': {
            enum4 = 6;
            break;
          }
          case 'capability-unavailable': {
            enum4 = 7;
            break;
          }
          case 'unsupported-surface': {
            enum4 = 8;
            break;
          }
          case 'resource-limit': {
            enum4 = 9;
            break;
          }
          case 'deadline-exceeded': {
            enum4 = 10;
            break;
          }
          case 'cancelled': {
            enum4 = 11;
            break;
          }
          case 'app-disabled': {
            enum4 = 12;
            break;
          }
          case 'app-uninstalled': {
            enum4 = 13;
            break;
          }
          case 'upgrade-in-progress': {
            enum4 = 14;
            break;
          }
          case 'integrity-failure': {
            enum4 = 15;
            break;
          }
          case 'not-found': {
            enum4 = 16;
            break;
          }
          case 'conflict': {
            enum4 = 17;
            break;
          }
          case 'stale-revision': {
            enum4 = 18;
            break;
          }
          case 'malformed-output': {
            enum4 = 19;
            break;
          }
          case 'forged-identifier': {
            enum4 = 20;
            break;
          }
          case 'internal': {
            enum4 = 21;
            break;
          }
          default: {
            if ((v3_0) instanceof Error) {
              console.error(v3_0);
            }
            
            throw new TypeError(`"${val4}" is not one of the cases of error-code`);
          }
        }
        dataView(memory0).setInt8(arg0 + 4, enum4, true);
        var ptr5 = utf8Encode(v3_1, realloc0, memory0);
        var len5 = utf8EncodedLen;
        dataView(memory0).setUint32(arg0 + 12, len5, true);
        dataView(memory0).setUint32(arg0 + 8, ptr5, true);
        dataView(memory0).setInt8(arg0 + 16, v3_2 ? 1 : 0, true);
        break;
      }
      default: {
        throw new TypeError('invalid variant specified for result');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/clock@0.0.1", function="wall-now"][Instruction::Return]', {
      funcName: 'wall-now',
      paramCount: 0,
      async: false,
      postReturn: false
    });
  }
  
  
  function trampoline2(arg0, arg1, arg2, arg3) {
    var ptr0 = arg0;
    var len0 = arg1;
    var result0 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr0, len0));
    _debugLog('[iface="vibapp:experimental-v0/kv@0.0.1", function="scan-prefix"] [Instruction::CallInterface] (async? sync, @ enter)');
    const _interface_call_currentTaskID = startCurrentTask(0, false, 'scan-prefix');
    let ret;
    try {
      ret = { tag: 'ok', val: scanPrefix(result0, arg2 >>> 0)};
    } catch (e) {
      ret = { tag: 'err', val: getErrorPayload(e) };
    }
    _debugLog('[iface="vibapp:experimental-v0/kv@0.0.1", function="scan-prefix"] [Instruction::CallInterface] (sync, @ post-call)');
    endCurrentTask(0);
    var variant8 = ret;
    switch (variant8.tag) {
      case 'ok': {
        const e = variant8.val;
        dataView(memory0).setInt8(arg3 + 0, 0, true);
        var vec4 = e;
        var len4 = vec4.length;
        var result4 = realloc0(0, 0, 8, len4 * 24);
        for (let i = 0; i < vec4.length; i++) {
          const e = vec4[i];
          const base = result4 + i * 24;var {key: v1_0, value: v1_1, revision: v1_2 } = e;
          var ptr2 = utf8Encode(v1_0, realloc0, memory0);
          var len2 = utf8EncodedLen;
          dataView(memory0).setUint32(base + 4, len2, true);
          dataView(memory0).setUint32(base + 0, ptr2, true);
          var val3 = v1_1;
          var len3 = val3.byteLength;
          var ptr3 = realloc0(0, 0, 1, len3 * 1);
          var src3 = new Uint8Array(val3.buffer || val3, val3.byteOffset, len3 * 1);
          (new Uint8Array(memory0.buffer, ptr3, len3 * 1)).set(src3);
          dataView(memory0).setUint32(base + 12, len3, true);
          dataView(memory0).setUint32(base + 8, ptr3, true);
          dataView(memory0).setBigInt64(base + 16, toUint64(v1_2), true);
        }
        dataView(memory0).setUint32(arg3 + 8, len4, true);
        dataView(memory0).setUint32(arg3 + 4, result4, true);
        break;
      }
      case 'err': {
        const e = variant8.val;
        dataView(memory0).setInt8(arg3 + 0, 1, true);
        var {code: v5_0, message: v5_1, retryable: v5_2 } = e;
        var val6 = v5_0;
        let enum6;
        switch (val6) {
          case 'invalid-argument': {
            enum6 = 0;
            break;
          }
          case 'incompatible-contract': {
            enum6 = 1;
            break;
          }
          case 'unsupported-version': {
            enum6 = 2;
            break;
          }
          case 'unknown-interface': {
            enum6 = 3;
            break;
          }
          case 'missing-interface': {
            enum6 = 4;
            break;
          }
          case 'permission-denied': {
            enum6 = 5;
            break;
          }
          case 'consent-required': {
            enum6 = 6;
            break;
          }
          case 'capability-unavailable': {
            enum6 = 7;
            break;
          }
          case 'unsupported-surface': {
            enum6 = 8;
            break;
          }
          case 'resource-limit': {
            enum6 = 9;
            break;
          }
          case 'deadline-exceeded': {
            enum6 = 10;
            break;
          }
          case 'cancelled': {
            enum6 = 11;
            break;
          }
          case 'app-disabled': {
            enum6 = 12;
            break;
          }
          case 'app-uninstalled': {
            enum6 = 13;
            break;
          }
          case 'upgrade-in-progress': {
            enum6 = 14;
            break;
          }
          case 'integrity-failure': {
            enum6 = 15;
            break;
          }
          case 'not-found': {
            enum6 = 16;
            break;
          }
          case 'conflict': {
            enum6 = 17;
            break;
          }
          case 'stale-revision': {
            enum6 = 18;
            break;
          }
          case 'malformed-output': {
            enum6 = 19;
            break;
          }
          case 'forged-identifier': {
            enum6 = 20;
            break;
          }
          case 'internal': {
            enum6 = 21;
            break;
          }
          default: {
            if ((v5_0) instanceof Error) {
              console.error(v5_0);
            }
            
            throw new TypeError(`"${val6}" is not one of the cases of error-code`);
          }
        }
        dataView(memory0).setInt8(arg3 + 4, enum6, true);
        var ptr7 = utf8Encode(v5_1, realloc0, memory0);
        var len7 = utf8EncodedLen;
        dataView(memory0).setUint32(arg3 + 12, len7, true);
        dataView(memory0).setUint32(arg3 + 8, ptr7, true);
        dataView(memory0).setInt8(arg3 + 16, v5_2 ? 1 : 0, true);
        break;
      }
      default: {
        throw new TypeError('invalid variant specified for result');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/kv@0.0.1", function="scan-prefix"][Instruction::Return]', {
      funcName: 'scan-prefix',
      paramCount: 0,
      async: false,
      postReturn: false
    });
  }
  
  
  function trampoline3(arg0, arg1, arg2, arg3, arg4, arg5, arg6, arg7) {
    let enum0;
    switch (arg0) {
      case 0: {
        enum0 = 'debug';
        break;
      }
      case 1: {
        enum0 = 'info';
        break;
      }
      case 2: {
        enum0 = 'warn';
        break;
      }
      case 3: {
        enum0 = 'error';
        break;
      }
      default: {
        throw new TypeError('invalid discriminant specified for Level');
      }
    }
    var ptr1 = arg1;
    var len1 = arg2;
    var result1 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr1, len1));
    var len4 = arg4;
    var base4 = arg3;
    var result4 = [];
    for (let i = 0; i < len4; i++) {
      const base = base4 + i * 16;
      var ptr2 = dataView(memory0).getUint32(base + 0, true);
      var len2 = dataView(memory0).getUint32(base + 4, true);
      var result2 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr2, len2));
      var ptr3 = dataView(memory0).getUint32(base + 8, true);
      var len3 = dataView(memory0).getUint32(base + 12, true);
      var result3 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr3, len3));
      result4.push({
        key: result2,
        value: result3,
      });
    }
    var ptr5 = arg5;
    var len5 = arg6;
    var result5 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr5, len5));
    _debugLog('[iface="vibapp:experimental-v0/log@0.0.1", function="write"] [Instruction::CallInterface] (async? sync, @ enter)');
    const _interface_call_currentTaskID = startCurrentTask(0, false, 'write');
    let ret;
    try {
      ret = { tag: 'ok', val: write(enum0, result1, result4, result5)};
    } catch (e) {
      ret = { tag: 'err', val: getErrorPayload(e) };
    }
    _debugLog('[iface="vibapp:experimental-v0/log@0.0.1", function="write"] [Instruction::CallInterface] (sync, @ post-call)');
    endCurrentTask(0);
    var variant9 = ret;
    switch (variant9.tag) {
      case 'ok': {
        const e = variant9.val;
        dataView(memory0).setInt8(arg7 + 0, 0, true);
        break;
      }
      case 'err': {
        const e = variant9.val;
        dataView(memory0).setInt8(arg7 + 0, 1, true);
        var {code: v6_0, message: v6_1, retryable: v6_2 } = e;
        var val7 = v6_0;
        let enum7;
        switch (val7) {
          case 'invalid-argument': {
            enum7 = 0;
            break;
          }
          case 'incompatible-contract': {
            enum7 = 1;
            break;
          }
          case 'unsupported-version': {
            enum7 = 2;
            break;
          }
          case 'unknown-interface': {
            enum7 = 3;
            break;
          }
          case 'missing-interface': {
            enum7 = 4;
            break;
          }
          case 'permission-denied': {
            enum7 = 5;
            break;
          }
          case 'consent-required': {
            enum7 = 6;
            break;
          }
          case 'capability-unavailable': {
            enum7 = 7;
            break;
          }
          case 'unsupported-surface': {
            enum7 = 8;
            break;
          }
          case 'resource-limit': {
            enum7 = 9;
            break;
          }
          case 'deadline-exceeded': {
            enum7 = 10;
            break;
          }
          case 'cancelled': {
            enum7 = 11;
            break;
          }
          case 'app-disabled': {
            enum7 = 12;
            break;
          }
          case 'app-uninstalled': {
            enum7 = 13;
            break;
          }
          case 'upgrade-in-progress': {
            enum7 = 14;
            break;
          }
          case 'integrity-failure': {
            enum7 = 15;
            break;
          }
          case 'not-found': {
            enum7 = 16;
            break;
          }
          case 'conflict': {
            enum7 = 17;
            break;
          }
          case 'stale-revision': {
            enum7 = 18;
            break;
          }
          case 'malformed-output': {
            enum7 = 19;
            break;
          }
          case 'forged-identifier': {
            enum7 = 20;
            break;
          }
          case 'internal': {
            enum7 = 21;
            break;
          }
          default: {
            if ((v6_0) instanceof Error) {
              console.error(v6_0);
            }
            
            throw new TypeError(`"${val7}" is not one of the cases of error-code`);
          }
        }
        dataView(memory0).setInt8(arg7 + 4, enum7, true);
        var ptr8 = utf8Encode(v6_1, realloc0, memory0);
        var len8 = utf8EncodedLen;
        dataView(memory0).setUint32(arg7 + 12, len8, true);
        dataView(memory0).setUint32(arg7 + 8, ptr8, true);
        dataView(memory0).setInt8(arg7 + 16, v6_2 ? 1 : 0, true);
        break;
      }
      default: {
        throw new TypeError('invalid variant specified for result');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/log@0.0.1", function="write"][Instruction::Return]', {
      funcName: 'write',
      paramCount: 0,
      async: false,
      postReturn: false
    });
  }
  
  
  function trampoline4(arg0) {
    _debugLog('[iface="vibapp:experimental-v0/host-info@0.0.1", function="describe-host"] [Instruction::CallInterface] (async? sync, @ enter)');
    const _interface_call_currentTaskID = startCurrentTask(0, false, 'describe-host');
    const ret = describeHost();
    _debugLog('[iface="vibapp:experimental-v0/host-info@0.0.1", function="describe-host"] [Instruction::CallInterface] (sync, @ post-call)');
    endCurrentTask(0);
    var {profile: v0_0, background: v0_1, capabilities: v0_2 } = ret;
    var val1 = v0_0;
    let enum1;
    switch (val1) {
      case 'desktop': {
        enum1 = 0;
        break;
      }
      case 'web-preview': {
        enum1 = 1;
        break;
      }
      case 'web-runtime': {
        enum1 = 2;
        break;
      }
      case 'headless': {
        enum1 = 3;
        break;
      }
      default: {
        if ((v0_0) instanceof Error) {
          console.error(v0_0);
        }
        
        throw new TypeError(`"${val1}" is not one of the cases of execution-profile`);
      }
    }
    dataView(memory0).setInt8(arg0 + 0, enum1, true);
    var val2 = v0_1;
    let enum2;
    switch (val2) {
      case 'daemon': {
        enum2 = 0;
        break;
      }
      case 'process': {
        enum2 = 1;
        break;
      }
      case 'foreground-only': {
        enum2 = 2;
        break;
      }
      case 'not-applicable': {
        enum2 = 3;
        break;
      }
      default: {
        if ((v0_1) instanceof Error) {
          console.error(v0_1);
        }
        
        throw new TypeError(`"${val2}" is not one of the cases of background-reliability`);
      }
    }
    dataView(memory0).setInt8(arg0 + 1, enum2, true);
    var vec7 = v0_2;
    var len7 = vec7.length;
    var result7 = realloc0(0, 0, 4, len7 * 20);
    for (let i = 0; i < vec7.length; i++) {
      const e = vec7[i];
      const base = result7 + i * 20;var {interfaceName: v3_0, availability: v3_1, granted: v3_2, detail: v3_3 } = e;
      var ptr4 = utf8Encode(v3_0, realloc0, memory0);
      var len4 = utf8EncodedLen;
      dataView(memory0).setUint32(base + 4, len4, true);
      dataView(memory0).setUint32(base + 0, ptr4, true);
      var val5 = v3_1;
      let enum5;
      switch (val5) {
        case 'native': {
          enum5 = 0;
          break;
        }
        case 'brokered': {
          enum5 = 1;
          break;
        }
        case 'mock': {
          enum5 = 2;
          break;
        }
        case 'denied': {
          enum5 = 3;
          break;
        }
        case 'unavailable': {
          enum5 = 4;
          break;
        }
        default: {
          if ((v3_1) instanceof Error) {
            console.error(v3_1);
          }
          
          throw new TypeError(`"${val5}" is not one of the cases of availability`);
        }
      }
      dataView(memory0).setInt8(base + 8, enum5, true);
      dataView(memory0).setInt8(base + 9, v3_2 ? 1 : 0, true);
      var ptr6 = utf8Encode(v3_3, realloc0, memory0);
      var len6 = utf8EncodedLen;
      dataView(memory0).setUint32(base + 16, len6, true);
      dataView(memory0).setUint32(base + 12, ptr6, true);
    }
    dataView(memory0).setUint32(arg0 + 8, len7, true);
    dataView(memory0).setUint32(arg0 + 4, result7, true);
    _debugLog('[iface="vibapp:experimental-v0/host-info@0.0.1", function="describe-host"][Instruction::Return]', {
      funcName: 'describe-host',
      paramCount: 0,
      async: false,
      postReturn: false
    });
  }
  
  
  function trampoline5(arg0) {
    _debugLog('[iface="vibapp:experimental-v0/settings@0.0.1", function="current"] [Instruction::CallInterface] (async? sync, @ enter)');
    const _interface_call_currentTaskID = startCurrentTask(0, false, 'current');
    let ret;
    try {
      ret = { tag: 'ok', val: current()};
    } catch (e) {
      ret = { tag: 'err', val: getErrorPayload(e) };
    }
    _debugLog('[iface="vibapp:experimental-v0/settings@0.0.1", function="current"] [Instruction::CallInterface] (sync, @ post-call)');
    endCurrentTask(0);
    var variant15 = ret;
    switch (variant15.tag) {
      case 'ok': {
        const e = variant15.val;
        dataView(memory0).setInt8(arg0 + 0, 0, true);
        var {schemaRevision: v0_0, configRevision: v0_1, values: v0_2 } = e;
        dataView(memory0).setBigInt64(arg0 + 8, toUint64(v0_0), true);
        dataView(memory0).setBigInt64(arg0 + 16, toUint64(v0_1), true);
        var vec11 = v0_2;
        var len11 = vec11.length;
        var result11 = realloc0(0, 0, 8, len11 * 24);
        for (let i = 0; i < vec11.length; i++) {
          const e = vec11[i];
          const base = result11 + i * 24;var {key: v1_0, value: v1_1 } = e;
          var ptr2 = utf8Encode(v1_0, realloc0, memory0);
          var len2 = utf8EncodedLen;
          dataView(memory0).setUint32(base + 4, len2, true);
          dataView(memory0).setUint32(base + 0, ptr2, true);
          var variant10 = v1_1;
          switch (variant10.tag) {
            case 'empty': {
              dataView(memory0).setInt8(base + 8, 0, true);
              break;
            }
            case 'text': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 1, true);
              var ptr3 = utf8Encode(e, realloc0, memory0);
              var len3 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 20, len3, true);
              dataView(memory0).setUint32(base + 16, ptr3, true);
              break;
            }
            case 'secret-handle': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 2, true);
              var ptr4 = utf8Encode(e, realloc0, memory0);
              var len4 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 20, len4, true);
              dataView(memory0).setUint32(base + 16, ptr4, true);
              break;
            }
            case 'integer': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 3, true);
              dataView(memory0).setBigInt64(base + 16, toInt64(e), true);
              break;
            }
            case 'decimal': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 4, true);
              var ptr5 = utf8Encode(e, realloc0, memory0);
              var len5 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 20, len5, true);
              dataView(memory0).setUint32(base + 16, ptr5, true);
              break;
            }
            case 'boolean': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 5, true);
              dataView(memory0).setInt8(base + 16, e ? 1 : 0, true);
              break;
            }
            case 'date': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 6, true);
              var {year: v6_0, month: v6_1, day: v6_2 } = e;
              dataView(memory0).setInt16(base + 16, toUint16(v6_0), true);
              dataView(memory0).setInt8(base + 18, toUint8(v6_1), true);
              dataView(memory0).setInt8(base + 19, toUint8(v6_2), true);
              break;
            }
            case 'time': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 7, true);
              var {hour: v7_0, minute: v7_1, second: v7_2 } = e;
              dataView(memory0).setInt8(base + 16, toUint8(v7_0), true);
              dataView(memory0).setInt8(base + 17, toUint8(v7_1), true);
              dataView(memory0).setInt8(base + 18, toUint8(v7_2), true);
              break;
            }
            case 'time-zone': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 8, true);
              var ptr8 = utf8Encode(e, realloc0, memory0);
              var len8 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 20, len8, true);
              dataView(memory0).setUint32(base + 16, ptr8, true);
              break;
            }
            case 'choice': {
              const e = variant10.val;
              dataView(memory0).setInt8(base + 8, 9, true);
              var ptr9 = utf8Encode(e, realloc0, memory0);
              var len9 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 20, len9, true);
              dataView(memory0).setUint32(base + 16, ptr9, true);
              break;
            }
            default: {
              throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant10.tag)}\` (received \`${variant10}\`) specified for \`FieldValue\``);
            }
          }
        }
        dataView(memory0).setUint32(arg0 + 28, len11, true);
        dataView(memory0).setUint32(arg0 + 24, result11, true);
        break;
      }
      case 'err': {
        const e = variant15.val;
        dataView(memory0).setInt8(arg0 + 0, 1, true);
        var {code: v12_0, message: v12_1, retryable: v12_2 } = e;
        var val13 = v12_0;
        let enum13;
        switch (val13) {
          case 'invalid-argument': {
            enum13 = 0;
            break;
          }
          case 'incompatible-contract': {
            enum13 = 1;
            break;
          }
          case 'unsupported-version': {
            enum13 = 2;
            break;
          }
          case 'unknown-interface': {
            enum13 = 3;
            break;
          }
          case 'missing-interface': {
            enum13 = 4;
            break;
          }
          case 'permission-denied': {
            enum13 = 5;
            break;
          }
          case 'consent-required': {
            enum13 = 6;
            break;
          }
          case 'capability-unavailable': {
            enum13 = 7;
            break;
          }
          case 'unsupported-surface': {
            enum13 = 8;
            break;
          }
          case 'resource-limit': {
            enum13 = 9;
            break;
          }
          case 'deadline-exceeded': {
            enum13 = 10;
            break;
          }
          case 'cancelled': {
            enum13 = 11;
            break;
          }
          case 'app-disabled': {
            enum13 = 12;
            break;
          }
          case 'app-uninstalled': {
            enum13 = 13;
            break;
          }
          case 'upgrade-in-progress': {
            enum13 = 14;
            break;
          }
          case 'integrity-failure': {
            enum13 = 15;
            break;
          }
          case 'not-found': {
            enum13 = 16;
            break;
          }
          case 'conflict': {
            enum13 = 17;
            break;
          }
          case 'stale-revision': {
            enum13 = 18;
            break;
          }
          case 'malformed-output': {
            enum13 = 19;
            break;
          }
          case 'forged-identifier': {
            enum13 = 20;
            break;
          }
          case 'internal': {
            enum13 = 21;
            break;
          }
          default: {
            if ((v12_0) instanceof Error) {
              console.error(v12_0);
            }
            
            throw new TypeError(`"${val13}" is not one of the cases of error-code`);
          }
        }
        dataView(memory0).setInt8(arg0 + 8, enum13, true);
        var ptr14 = utf8Encode(v12_1, realloc0, memory0);
        var len14 = utf8EncodedLen;
        dataView(memory0).setUint32(arg0 + 16, len14, true);
        dataView(memory0).setUint32(arg0 + 12, ptr14, true);
        dataView(memory0).setInt8(arg0 + 20, v12_2 ? 1 : 0, true);
        break;
      }
      default: {
        throw new TypeError('invalid variant specified for result');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/settings@0.0.1", function="current"][Instruction::Return]', {
      funcName: 'current',
      paramCount: 0,
      async: false,
      postReturn: false
    });
  }
  
  let exports2;
  let postReturn0;
  let postReturn1;
  let postReturn2;
  ({ exports: exports0 } = instantiateCore(module1));
  ({ exports: exports1 } = instantiateCore(module0, {
    'vibapp:experimental-v0/clock@0.0.1': {
      'monotonic-now': trampoline0,
      'wall-now': exports0['0'],
    },
    'vibapp:experimental-v0/host-info@0.0.1': {
      'describe-host': exports0['3'],
    },
    'vibapp:experimental-v0/kv@0.0.1': {
      'scan-prefix': exports0['1'],
    },
    'vibapp:experimental-v0/log@0.0.1': {
      write: exports0['2'],
    },
    'vibapp:experimental-v0/settings@0.0.1': {
      current: exports0['4'],
    },
  }));
  memory0 = exports1.memory;
  realloc0 = exports1.cabi_realloc;
  ({ exports: exports2 } = instantiateCore(module2, {
    '': {
      $imports: exports0.$imports,
      '0': trampoline1,
      '1': trampoline2,
      '2': trampoline3,
      '3': trampoline4,
      '4': trampoline5,
    },
  }));
  postReturn0 = exports1['cabi_post_vibapp:experimental-v0/guest@0.0.1#describe'];
  postReturn1 = exports1['cabi_post_vibapp:experimental-v0/guest@0.0.1#handle-event'];
  postReturn2 = exports1['cabi_post_vibapp:experimental-v0/guest@0.0.1#migrate'];
  let guest001Describe;
  
  function describe() {
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="describe"][Instruction::CallWasm] enter', {
      funcName: 'describe',
      paramCount: 0,
      async: false,
      postReturn: true,
    });
    const _wasm_call_currentTaskID = startCurrentTask(0, false, 'guest001Describe');
    const ret = guest001Describe();
    endCurrentTask(0);
    let variant13;
    switch (dataView(memory0).getUint8(ret + 0, true)) {
      case 0: {
        var ptr0 = dataView(memory0).getUint32(ret + 4, true);
        var len0 = dataView(memory0).getUint32(ret + 8, true);
        var result0 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr0, len0));
        var ptr1 = dataView(memory0).getUint32(ret + 12, true);
        var len1 = dataView(memory0).getUint32(ret + 16, true);
        var result1 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr1, len1));
        let enum2;
        switch (dataView(memory0).getUint8(ret + 20, true)) {
          case 0: {
            enum2 = 'ui';
            break;
          }
          case 1: {
            enum2 = 'service';
            break;
          }
          case 2: {
            enum2 = 'hybrid';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for AppKind');
          }
        }
        var ptr3 = dataView(memory0).getUint32(ret + 24, true);
        var len3 = dataView(memory0).getUint32(ret + 28, true);
        var result3 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr3, len3));
        var len9 = dataView(memory0).getUint32(ret + 36, true);
        var base9 = dataView(memory0).getUint32(ret + 32, true);
        var result9 = [];
        for (let i = 0; i < len9; i++) {
          const base = base9 + i * 32;
          var ptr4 = dataView(memory0).getUint32(base + 0, true);
          var len4 = dataView(memory0).getUint32(base + 4, true);
          var result4 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr4, len4));
          let enum5;
          switch (dataView(memory0).getUint8(base + 8, true)) {
            case 0: {
              enum5 = 'launcher-ui';
              break;
            }
            case 1: {
              enum5 = 'service';
              break;
            }
            case 2: {
              enum5 = 'settings';
              break;
            }
            default: {
              throw new TypeError('invalid discriminant specified for EntrypointKind');
            }
          }
          var ptr6 = dataView(memory0).getUint32(base + 12, true);
          var len6 = dataView(memory0).getUint32(base + 16, true);
          var result6 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr6, len6));
          let variant8;
          switch (dataView(memory0).getUint8(base + 20, true)) {
            case 0: {
              variant8 = undefined;
              break;
            }
            case 1: {
              var ptr7 = dataView(memory0).getUint32(base + 24, true);
              var len7 = dataView(memory0).getUint32(base + 28, true);
              var result7 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr7, len7));
              variant8 = result7;
              break;
            }
            default: {
              throw new TypeError('invalid variant discriminant for option');
            }
          }
          result9.push({
            id: result4,
            kind: enum5,
            label: result6,
            initialRoute: variant8,
          });
        }
        variant13= {
          tag: 'ok',
          val: {
            id: result0,
            version: result1,
            kind: enum2,
            displayName: result3,
            entrypoints: result9,
          }
        };
        break;
      }
      case 1: {
        let enum10;
        switch (dataView(memory0).getUint8(ret + 4, true)) {
          case 0: {
            enum10 = 'invalid-argument';
            break;
          }
          case 1: {
            enum10 = 'incompatible-contract';
            break;
          }
          case 2: {
            enum10 = 'unsupported-version';
            break;
          }
          case 3: {
            enum10 = 'unknown-interface';
            break;
          }
          case 4: {
            enum10 = 'missing-interface';
            break;
          }
          case 5: {
            enum10 = 'permission-denied';
            break;
          }
          case 6: {
            enum10 = 'consent-required';
            break;
          }
          case 7: {
            enum10 = 'capability-unavailable';
            break;
          }
          case 8: {
            enum10 = 'unsupported-surface';
            break;
          }
          case 9: {
            enum10 = 'resource-limit';
            break;
          }
          case 10: {
            enum10 = 'deadline-exceeded';
            break;
          }
          case 11: {
            enum10 = 'cancelled';
            break;
          }
          case 12: {
            enum10 = 'app-disabled';
            break;
          }
          case 13: {
            enum10 = 'app-uninstalled';
            break;
          }
          case 14: {
            enum10 = 'upgrade-in-progress';
            break;
          }
          case 15: {
            enum10 = 'integrity-failure';
            break;
          }
          case 16: {
            enum10 = 'not-found';
            break;
          }
          case 17: {
            enum10 = 'conflict';
            break;
          }
          case 18: {
            enum10 = 'stale-revision';
            break;
          }
          case 19: {
            enum10 = 'malformed-output';
            break;
          }
          case 20: {
            enum10 = 'forged-identifier';
            break;
          }
          case 21: {
            enum10 = 'internal';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for ErrorCode');
          }
        }
        var ptr11 = dataView(memory0).getUint32(ret + 8, true);
        var len11 = dataView(memory0).getUint32(ret + 12, true);
        var result11 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr11, len11));
        var bool12 = dataView(memory0).getUint8(ret + 16, true);
        variant13= {
          tag: 'err',
          val: {
            code: enum10,
            message: result11,
            retryable: bool12 == 0 ? false : (bool12 == 1 ? true : throwInvalidBool()),
          }
        };
        break;
      }
      default: {
        throw new TypeError('invalid variant discriminant for expected');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="describe"][Instruction::Return]', {
      funcName: 'describe',
      paramCount: 1,
      async: false,
      postReturn: true
    });
    const retCopy = variant13;
    
    let cstate = getOrCreateAsyncState(0);
    cstate.mayLeave = false;
    postReturn0(ret);
    cstate.mayLeave = true;
    
    
    
    if (typeof retCopy === 'object' && retCopy.tag === 'err') {
      throw new ComponentError(retCopy.val);
    }
    return retCopy.val;
    
  }
  let guest001GetSettingsSchema;
  
  function getSettingsSchema() {
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="get-settings-schema"][Instruction::CallWasm] enter', {
      funcName: 'get-settings-schema',
      paramCount: 0,
      async: false,
      postReturn: true,
    });
    const _wasm_call_currentTaskID = startCurrentTask(0, false, 'guest001GetSettingsSchema');
    const ret = guest001GetSettingsSchema();
    endCurrentTask(0);
    let variant22;
    switch (dataView(memory0).getUint8(ret + 0, true)) {
      case 0: {
        let variant18;
        switch (dataView(memory0).getUint8(ret + 8, true)) {
          case 0: {
            variant18 = undefined;
            break;
          }
          case 1: {
            var len17 = dataView(memory0).getUint32(ret + 28, true);
            var base17 = dataView(memory0).getUint32(ret + 24, true);
            var result17 = [];
            for (let i = 0; i < len17; i++) {
              const base = base17 + i * 64;
              var ptr0 = dataView(memory0).getUint32(base + 0, true);
              var len0 = dataView(memory0).getUint32(base + 4, true);
              var result0 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr0, len0));
              var ptr1 = dataView(memory0).getUint32(base + 8, true);
              var len1 = dataView(memory0).getUint32(base + 12, true);
              var result1 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr1, len1));
              var ptr2 = dataView(memory0).getUint32(base + 16, true);
              var len2 = dataView(memory0).getUint32(base + 20, true);
              var result2 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr2, len2));
              let enum3;
              switch (dataView(memory0).getUint8(base + 24, true)) {
                case 0: {
                  enum3 = 'text';
                  break;
                }
                case 1: {
                  enum3 = 'integer';
                  break;
                }
                case 2: {
                  enum3 = 'decimal';
                  break;
                }
                case 3: {
                  enum3 = 'boolean';
                  break;
                }
                case 4: {
                  enum3 = 'date';
                  break;
                }
                case 5: {
                  enum3 = 'time';
                  break;
                }
                case 6: {
                  enum3 = 'time-zone';
                  break;
                }
                case 7: {
                  enum3 = 'choice';
                  break;
                }
                default: {
                  throw new TypeError('invalid discriminant specified for FieldKind');
                }
              }
              var bool4 = dataView(memory0).getUint8(base + 25, true);
              var bool5 = dataView(memory0).getUint8(base + 26, true);
              let variant13;
              switch (dataView(memory0).getUint8(base + 32, true)) {
                case 0: {
                  variant13 = undefined;
                  break;
                }
                case 1: {
                  let variant12;
                  switch (dataView(memory0).getUint8(base + 40, true)) {
                    case 0: {
                      variant12= {
                        tag: 'empty',
                      };
                      break;
                    }
                    case 1: {
                      var ptr6 = dataView(memory0).getUint32(base + 48, true);
                      var len6 = dataView(memory0).getUint32(base + 52, true);
                      var result6 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr6, len6));
                      variant12= {
                        tag: 'text',
                        val: result6
                      };
                      break;
                    }
                    case 2: {
                      var ptr7 = dataView(memory0).getUint32(base + 48, true);
                      var len7 = dataView(memory0).getUint32(base + 52, true);
                      var result7 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr7, len7));
                      variant12= {
                        tag: 'secret-handle',
                        val: result7
                      };
                      break;
                    }
                    case 3: {
                      variant12= {
                        tag: 'integer',
                        val: dataView(memory0).getBigInt64(base + 48, true)
                      };
                      break;
                    }
                    case 4: {
                      var ptr8 = dataView(memory0).getUint32(base + 48, true);
                      var len8 = dataView(memory0).getUint32(base + 52, true);
                      var result8 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr8, len8));
                      variant12= {
                        tag: 'decimal',
                        val: result8
                      };
                      break;
                    }
                    case 5: {
                      var bool9 = dataView(memory0).getUint8(base + 48, true);
                      variant12= {
                        tag: 'boolean',
                        val: bool9 == 0 ? false : (bool9 == 1 ? true : throwInvalidBool())
                      };
                      break;
                    }
                    case 6: {
                      variant12= {
                        tag: 'date',
                        val: {
                          year: clampGuest(dataView(memory0).getUint16(base + 48, true), 0, 65535),
                          month: clampGuest(dataView(memory0).getUint8(base + 50, true), 0, 255),
                          day: clampGuest(dataView(memory0).getUint8(base + 51, true), 0, 255),
                        }
                      };
                      break;
                    }
                    case 7: {
                      variant12= {
                        tag: 'time',
                        val: {
                          hour: clampGuest(dataView(memory0).getUint8(base + 48, true), 0, 255),
                          minute: clampGuest(dataView(memory0).getUint8(base + 49, true), 0, 255),
                          second: clampGuest(dataView(memory0).getUint8(base + 50, true), 0, 255),
                        }
                      };
                      break;
                    }
                    case 8: {
                      var ptr10 = dataView(memory0).getUint32(base + 48, true);
                      var len10 = dataView(memory0).getUint32(base + 52, true);
                      var result10 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr10, len10));
                      variant12= {
                        tag: 'time-zone',
                        val: result10
                      };
                      break;
                    }
                    case 9: {
                      var ptr11 = dataView(memory0).getUint32(base + 48, true);
                      var len11 = dataView(memory0).getUint32(base + 52, true);
                      var result11 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr11, len11));
                      variant12= {
                        tag: 'choice',
                        val: result11
                      };
                      break;
                    }
                    default: {
                      throw new TypeError('invalid variant discriminant for FieldValue');
                    }
                  }
                  variant13 = variant12;
                  break;
                }
                default: {
                  throw new TypeError('invalid variant discriminant for option');
                }
              }
              var len16 = dataView(memory0).getUint32(base + 60, true);
              var base16 = dataView(memory0).getUint32(base + 56, true);
              var result16 = [];
              for (let i = 0; i < len16; i++) {
                const base = base16 + i * 16;
                var ptr14 = dataView(memory0).getUint32(base + 0, true);
                var len14 = dataView(memory0).getUint32(base + 4, true);
                var result14 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr14, len14));
                var ptr15 = dataView(memory0).getUint32(base + 8, true);
                var len15 = dataView(memory0).getUint32(base + 12, true);
                var result15 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr15, len15));
                result16.push({
                  value: result14,
                  label: result15,
                });
              }
              result17.push({
                key: result0,
                label: result1,
                description: result2,
                kind: enum3,
                required: bool4 == 0 ? false : (bool4 == 1 ? true : throwInvalidBool()),
                sensitive: bool5 == 0 ? false : (bool5 == 1 ? true : throwInvalidBool()),
                defaultValue: variant13,
                choices: result16,
              });
            }
            variant18 = {
              schemaRevision: BigInt.asUintN(64, dataView(memory0).getBigInt64(ret + 16, true)),
              settings: result17,
            };
            break;
          }
          default: {
            throw new TypeError('invalid variant discriminant for option');
          }
        }
        variant22= {
          tag: 'ok',
          val: variant18
        };
        break;
      }
      case 1: {
        let enum19;
        switch (dataView(memory0).getUint8(ret + 8, true)) {
          case 0: {
            enum19 = 'invalid-argument';
            break;
          }
          case 1: {
            enum19 = 'incompatible-contract';
            break;
          }
          case 2: {
            enum19 = 'unsupported-version';
            break;
          }
          case 3: {
            enum19 = 'unknown-interface';
            break;
          }
          case 4: {
            enum19 = 'missing-interface';
            break;
          }
          case 5: {
            enum19 = 'permission-denied';
            break;
          }
          case 6: {
            enum19 = 'consent-required';
            break;
          }
          case 7: {
            enum19 = 'capability-unavailable';
            break;
          }
          case 8: {
            enum19 = 'unsupported-surface';
            break;
          }
          case 9: {
            enum19 = 'resource-limit';
            break;
          }
          case 10: {
            enum19 = 'deadline-exceeded';
            break;
          }
          case 11: {
            enum19 = 'cancelled';
            break;
          }
          case 12: {
            enum19 = 'app-disabled';
            break;
          }
          case 13: {
            enum19 = 'app-uninstalled';
            break;
          }
          case 14: {
            enum19 = 'upgrade-in-progress';
            break;
          }
          case 15: {
            enum19 = 'integrity-failure';
            break;
          }
          case 16: {
            enum19 = 'not-found';
            break;
          }
          case 17: {
            enum19 = 'conflict';
            break;
          }
          case 18: {
            enum19 = 'stale-revision';
            break;
          }
          case 19: {
            enum19 = 'malformed-output';
            break;
          }
          case 20: {
            enum19 = 'forged-identifier';
            break;
          }
          case 21: {
            enum19 = 'internal';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for ErrorCode');
          }
        }
        var ptr20 = dataView(memory0).getUint32(ret + 12, true);
        var len20 = dataView(memory0).getUint32(ret + 16, true);
        var result20 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr20, len20));
        var bool21 = dataView(memory0).getUint8(ret + 20, true);
        variant22= {
          tag: 'err',
          val: {
            code: enum19,
            message: result20,
            retryable: bool21 == 0 ? false : (bool21 == 1 ? true : throwInvalidBool()),
          }
        };
        break;
      }
      default: {
        throw new TypeError('invalid variant discriminant for expected');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="get-settings-schema"][Instruction::Return]', {
      funcName: 'get-settings-schema',
      paramCount: 1,
      async: false,
      postReturn: true
    });
    const retCopy = variant22;
    
    let cstate = getOrCreateAsyncState(0);
    cstate.mayLeave = false;
    postReturn0(ret);
    cstate.mayLeave = true;
    
    
    
    if (typeof retCopy === 'object' && retCopy.tag === 'err') {
      throw new ComponentError(retCopy.val);
    }
    return retCopy.val;
    
  }
  let guest001ValidateSettings;
  
  function validateSettings(arg0, arg1) {
    var {eventId: v0_0, idempotencyKey: v0_1, cancellation: v0_2, generation: v0_3, profile: v0_4, deadlineMonotonicMs: v0_5 } = arg0;
    var ptr1 = utf8Encode(v0_0, realloc0, memory0);
    var len1 = utf8EncodedLen;
    var ptr2 = utf8Encode(v0_1, realloc0, memory0);
    var len2 = utf8EncodedLen;
    var ptr3 = utf8Encode(v0_2, realloc0, memory0);
    var len3 = utf8EncodedLen;
    var ptr4 = utf8Encode(v0_3, realloc0, memory0);
    var len4 = utf8EncodedLen;
    var val5 = v0_4;
    let enum5;
    switch (val5) {
      case 'desktop': {
        enum5 = 0;
        break;
      }
      case 'web-preview': {
        enum5 = 1;
        break;
      }
      case 'web-runtime': {
        enum5 = 2;
        break;
      }
      case 'headless': {
        enum5 = 3;
        break;
      }
      default: {
        if ((v0_4) instanceof Error) {
          console.error(v0_4);
        }
        
        throw new TypeError(`"${val5}" is not one of the cases of execution-profile`);
      }
    }
    var {schemaRevision: v6_0, configRevision: v6_1, values: v6_2 } = arg1;
    var vec17 = v6_2;
    var len17 = vec17.length;
    var result17 = realloc0(0, 0, 8, len17 * 24);
    for (let i = 0; i < vec17.length; i++) {
      const e = vec17[i];
      const base = result17 + i * 24;var {key: v7_0, value: v7_1 } = e;
      var ptr8 = utf8Encode(v7_0, realloc0, memory0);
      var len8 = utf8EncodedLen;
      dataView(memory0).setUint32(base + 4, len8, true);
      dataView(memory0).setUint32(base + 0, ptr8, true);
      var variant16 = v7_1;
      switch (variant16.tag) {
        case 'empty': {
          dataView(memory0).setInt8(base + 8, 0, true);
          break;
        }
        case 'text': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 1, true);
          var ptr9 = utf8Encode(e, realloc0, memory0);
          var len9 = utf8EncodedLen;
          dataView(memory0).setUint32(base + 20, len9, true);
          dataView(memory0).setUint32(base + 16, ptr9, true);
          break;
        }
        case 'secret-handle': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 2, true);
          var ptr10 = utf8Encode(e, realloc0, memory0);
          var len10 = utf8EncodedLen;
          dataView(memory0).setUint32(base + 20, len10, true);
          dataView(memory0).setUint32(base + 16, ptr10, true);
          break;
        }
        case 'integer': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 3, true);
          dataView(memory0).setBigInt64(base + 16, toInt64(e), true);
          break;
        }
        case 'decimal': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 4, true);
          var ptr11 = utf8Encode(e, realloc0, memory0);
          var len11 = utf8EncodedLen;
          dataView(memory0).setUint32(base + 20, len11, true);
          dataView(memory0).setUint32(base + 16, ptr11, true);
          break;
        }
        case 'boolean': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 5, true);
          dataView(memory0).setInt8(base + 16, e ? 1 : 0, true);
          break;
        }
        case 'date': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 6, true);
          var {year: v12_0, month: v12_1, day: v12_2 } = e;
          dataView(memory0).setInt16(base + 16, toUint16(v12_0), true);
          dataView(memory0).setInt8(base + 18, toUint8(v12_1), true);
          dataView(memory0).setInt8(base + 19, toUint8(v12_2), true);
          break;
        }
        case 'time': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 7, true);
          var {hour: v13_0, minute: v13_1, second: v13_2 } = e;
          dataView(memory0).setInt8(base + 16, toUint8(v13_0), true);
          dataView(memory0).setInt8(base + 17, toUint8(v13_1), true);
          dataView(memory0).setInt8(base + 18, toUint8(v13_2), true);
          break;
        }
        case 'time-zone': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 8, true);
          var ptr14 = utf8Encode(e, realloc0, memory0);
          var len14 = utf8EncodedLen;
          dataView(memory0).setUint32(base + 20, len14, true);
          dataView(memory0).setUint32(base + 16, ptr14, true);
          break;
        }
        case 'choice': {
          const e = variant16.val;
          dataView(memory0).setInt8(base + 8, 9, true);
          var ptr15 = utf8Encode(e, realloc0, memory0);
          var len15 = utf8EncodedLen;
          dataView(memory0).setUint32(base + 20, len15, true);
          dataView(memory0).setUint32(base + 16, ptr15, true);
          break;
        }
        default: {
          throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant16.tag)}\` (received \`${variant16}\`) specified for \`FieldValue\``);
        }
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="validate-settings"][Instruction::CallWasm] enter', {
      funcName: 'validate-settings',
      paramCount: 14,
      async: false,
      postReturn: true,
    });
    const _wasm_call_currentTaskID = startCurrentTask(0, false, 'guest001ValidateSettings');
    const ret = guest001ValidateSettings(ptr1, len1, ptr2, len2, ptr3, len3, ptr4, len4, enum5, toUint64(v0_5), toUint64(v6_0), toUint64(v6_1), result17, len17);
    endCurrentTask(0);
    let variant26;
    switch (dataView(memory0).getUint8(ret + 0, true)) {
      case 0: {
        var bool18 = dataView(memory0).getUint8(ret + 4, true);
        var len21 = dataView(memory0).getUint32(ret + 12, true);
        var base21 = dataView(memory0).getUint32(ret + 8, true);
        var result21 = [];
        for (let i = 0; i < len21; i++) {
          const base = base21 + i * 16;
          var ptr19 = dataView(memory0).getUint32(base + 0, true);
          var len19 = dataView(memory0).getUint32(base + 4, true);
          var result19 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr19, len19));
          var ptr20 = dataView(memory0).getUint32(base + 8, true);
          var len20 = dataView(memory0).getUint32(base + 12, true);
          var result20 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr20, len20));
          result21.push({
            key: result19,
            message: result20,
          });
        }
        var bool22 = dataView(memory0).getUint8(ret + 16, true);
        variant26= {
          tag: 'ok',
          val: {
            accepted: bool18 == 0 ? false : (bool18 == 1 ? true : throwInvalidBool()),
            fieldErrors: result21,
            serviceRestartRequired: bool22 == 0 ? false : (bool22 == 1 ? true : throwInvalidBool()),
          }
        };
        break;
      }
      case 1: {
        let enum23;
        switch (dataView(memory0).getUint8(ret + 4, true)) {
          case 0: {
            enum23 = 'invalid-argument';
            break;
          }
          case 1: {
            enum23 = 'incompatible-contract';
            break;
          }
          case 2: {
            enum23 = 'unsupported-version';
            break;
          }
          case 3: {
            enum23 = 'unknown-interface';
            break;
          }
          case 4: {
            enum23 = 'missing-interface';
            break;
          }
          case 5: {
            enum23 = 'permission-denied';
            break;
          }
          case 6: {
            enum23 = 'consent-required';
            break;
          }
          case 7: {
            enum23 = 'capability-unavailable';
            break;
          }
          case 8: {
            enum23 = 'unsupported-surface';
            break;
          }
          case 9: {
            enum23 = 'resource-limit';
            break;
          }
          case 10: {
            enum23 = 'deadline-exceeded';
            break;
          }
          case 11: {
            enum23 = 'cancelled';
            break;
          }
          case 12: {
            enum23 = 'app-disabled';
            break;
          }
          case 13: {
            enum23 = 'app-uninstalled';
            break;
          }
          case 14: {
            enum23 = 'upgrade-in-progress';
            break;
          }
          case 15: {
            enum23 = 'integrity-failure';
            break;
          }
          case 16: {
            enum23 = 'not-found';
            break;
          }
          case 17: {
            enum23 = 'conflict';
            break;
          }
          case 18: {
            enum23 = 'stale-revision';
            break;
          }
          case 19: {
            enum23 = 'malformed-output';
            break;
          }
          case 20: {
            enum23 = 'forged-identifier';
            break;
          }
          case 21: {
            enum23 = 'internal';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for ErrorCode');
          }
        }
        var ptr24 = dataView(memory0).getUint32(ret + 8, true);
        var len24 = dataView(memory0).getUint32(ret + 12, true);
        var result24 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr24, len24));
        var bool25 = dataView(memory0).getUint8(ret + 16, true);
        variant26= {
          tag: 'err',
          val: {
            code: enum23,
            message: result24,
            retryable: bool25 == 0 ? false : (bool25 == 1 ? true : throwInvalidBool()),
          }
        };
        break;
      }
      default: {
        throw new TypeError('invalid variant discriminant for expected');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="validate-settings"][Instruction::Return]', {
      funcName: 'validate-settings',
      paramCount: 1,
      async: false,
      postReturn: true
    });
    const retCopy = variant26;
    
    let cstate = getOrCreateAsyncState(0);
    cstate.mayLeave = false;
    postReturn0(ret);
    cstate.mayLeave = true;
    
    
    
    if (typeof retCopy === 'object' && retCopy.tag === 'err') {
      throw new ComponentError(retCopy.val);
    }
    return retCopy.val;
    
  }
  let guest001HandleEvent;
  
  function handleEvent(arg0, arg1) {
    var ptr0 = realloc0(0, 0, 8, 112);
    var {eventId: v1_0, idempotencyKey: v1_1, cancellation: v1_2, generation: v1_3, profile: v1_4, deadlineMonotonicMs: v1_5 } = arg0;
    var ptr2 = utf8Encode(v1_0, realloc0, memory0);
    var len2 = utf8EncodedLen;
    dataView(memory0).setUint32(ptr0 + 4, len2, true);
    dataView(memory0).setUint32(ptr0 + 0, ptr2, true);
    var ptr3 = utf8Encode(v1_1, realloc0, memory0);
    var len3 = utf8EncodedLen;
    dataView(memory0).setUint32(ptr0 + 12, len3, true);
    dataView(memory0).setUint32(ptr0 + 8, ptr3, true);
    var ptr4 = utf8Encode(v1_2, realloc0, memory0);
    var len4 = utf8EncodedLen;
    dataView(memory0).setUint32(ptr0 + 20, len4, true);
    dataView(memory0).setUint32(ptr0 + 16, ptr4, true);
    var ptr5 = utf8Encode(v1_3, realloc0, memory0);
    var len5 = utf8EncodedLen;
    dataView(memory0).setUint32(ptr0 + 28, len5, true);
    dataView(memory0).setUint32(ptr0 + 24, ptr5, true);
    var val6 = v1_4;
    let enum6;
    switch (val6) {
      case 'desktop': {
        enum6 = 0;
        break;
      }
      case 'web-preview': {
        enum6 = 1;
        break;
      }
      case 'web-runtime': {
        enum6 = 2;
        break;
      }
      case 'headless': {
        enum6 = 3;
        break;
      }
      default: {
        if ((v1_4) instanceof Error) {
          console.error(v1_4);
        }
        
        throw new TypeError(`"${val6}" is not one of the cases of execution-profile`);
      }
    }
    dataView(memory0).setInt8(ptr0 + 32, enum6, true);
    dataView(memory0).setBigInt64(ptr0 + 40, toUint64(v1_5), true);
    var variant95 = arg1;
    switch (variant95.tag) {
      case 'launcher': {
        const e = variant95.val;
        dataView(memory0).setInt8(ptr0 + 48, 0, true);
        var variant60 = e;
        switch (variant60.tag) {
          case 'launch': {
            const e = variant60.val;
            dataView(memory0).setInt8(ptr0 + 56, 0, true);
            var {entrypoint: v7_0, session: v7_1, surface: v7_2, route: v7_3, reason: v7_4 } = e;
            var ptr8 = utf8Encode(v7_0, realloc0, memory0);
            var len8 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len8, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr8, true);
            var ptr9 = utf8Encode(v7_1, realloc0, memory0);
            var len9 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len9, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr9, true);
            var ptr10 = utf8Encode(v7_2, realloc0, memory0);
            var len10 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 80, len10, true);
            dataView(memory0).setUint32(ptr0 + 76, ptr10, true);
            var ptr11 = utf8Encode(v7_3, realloc0, memory0);
            var len11 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 88, len11, true);
            dataView(memory0).setUint32(ptr0 + 84, ptr11, true);
            var val12 = v7_4;
            let enum12;
            switch (val12) {
              case 'user': {
                enum12 = 0;
                break;
              }
              case 'deep-link': {
                enum12 = 1;
                break;
              }
              case 'restore': {
                enum12 = 2;
                break;
              }
              default: {
                if ((v7_4) instanceof Error) {
                  console.error(v7_4);
                }
                
                throw new TypeError(`"${val12}" is not one of the cases of open-reason`);
              }
            }
            dataView(memory0).setInt8(ptr0 + 92, enum12, true);
            break;
          }
          case 'open': {
            const e = variant60.val;
            dataView(memory0).setInt8(ptr0 + 56, 1, true);
            var {entrypoint: v13_0, session: v13_1, surface: v13_2, route: v13_3, reason: v13_4 } = e;
            var ptr14 = utf8Encode(v13_0, realloc0, memory0);
            var len14 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len14, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr14, true);
            var ptr15 = utf8Encode(v13_1, realloc0, memory0);
            var len15 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len15, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr15, true);
            var ptr16 = utf8Encode(v13_2, realloc0, memory0);
            var len16 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 80, len16, true);
            dataView(memory0).setUint32(ptr0 + 76, ptr16, true);
            var ptr17 = utf8Encode(v13_3, realloc0, memory0);
            var len17 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 88, len17, true);
            dataView(memory0).setUint32(ptr0 + 84, ptr17, true);
            var val18 = v13_4;
            let enum18;
            switch (val18) {
              case 'user': {
                enum18 = 0;
                break;
              }
              case 'deep-link': {
                enum18 = 1;
                break;
              }
              case 'restore': {
                enum18 = 2;
                break;
              }
              default: {
                if ((v13_4) instanceof Error) {
                  console.error(v13_4);
                }
                
                throw new TypeError(`"${val18}" is not one of the cases of open-reason`);
              }
            }
            dataView(memory0).setInt8(ptr0 + 92, enum18, true);
            break;
          }
          case 'close': {
            const e = variant60.val;
            dataView(memory0).setInt8(ptr0 + 56, 2, true);
            var {entrypoint: v19_0, session: v19_1, surface: v19_2 } = e;
            var ptr20 = utf8Encode(v19_0, realloc0, memory0);
            var len20 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len20, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr20, true);
            var ptr21 = utf8Encode(v19_1, realloc0, memory0);
            var len21 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len21, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr21, true);
            var ptr22 = utf8Encode(v19_2, realloc0, memory0);
            var len22 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 80, len22, true);
            dataView(memory0).setUint32(ptr0 + 76, ptr22, true);
            break;
          }
          case 'focus': {
            const e = variant60.val;
            dataView(memory0).setInt8(ptr0 + 56, 3, true);
            var {entrypoint: v23_0, session: v23_1, surface: v23_2, focused: v23_3 } = e;
            var ptr24 = utf8Encode(v23_0, realloc0, memory0);
            var len24 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len24, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr24, true);
            var ptr25 = utf8Encode(v23_1, realloc0, memory0);
            var len25 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len25, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr25, true);
            var ptr26 = utf8Encode(v23_2, realloc0, memory0);
            var len26 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 80, len26, true);
            dataView(memory0).setUint32(ptr0 + 76, ptr26, true);
            dataView(memory0).setInt8(ptr0 + 84, v23_3 ? 1 : 0, true);
            break;
          }
          case 'restore': {
            const e = variant60.val;
            dataView(memory0).setInt8(ptr0 + 56, 4, true);
            var {entrypoint: v27_0, session: v27_1, surface: v27_2, route: v27_3, token: v27_4, safeFields: v27_5 } = e;
            var ptr28 = utf8Encode(v27_0, realloc0, memory0);
            var len28 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len28, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr28, true);
            var ptr29 = utf8Encode(v27_1, realloc0, memory0);
            var len29 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len29, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr29, true);
            var ptr30 = utf8Encode(v27_2, realloc0, memory0);
            var len30 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 80, len30, true);
            dataView(memory0).setUint32(ptr0 + 76, ptr30, true);
            var ptr31 = utf8Encode(v27_3, realloc0, memory0);
            var len31 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 88, len31, true);
            dataView(memory0).setUint32(ptr0 + 84, ptr31, true);
            var ptr32 = utf8Encode(v27_4, realloc0, memory0);
            var len32 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 96, len32, true);
            dataView(memory0).setUint32(ptr0 + 92, ptr32, true);
            var vec43 = v27_5;
            var len43 = vec43.length;
            var result43 = realloc0(0, 0, 8, len43 * 24);
            for (let i = 0; i < vec43.length; i++) {
              const e = vec43[i];
              const base = result43 + i * 24;var {field: v33_0, value: v33_1 } = e;
              var ptr34 = utf8Encode(v33_0, realloc0, memory0);
              var len34 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 4, len34, true);
              dataView(memory0).setUint32(base + 0, ptr34, true);
              var variant42 = v33_1;
              switch (variant42.tag) {
                case 'empty': {
                  dataView(memory0).setInt8(base + 8, 0, true);
                  break;
                }
                case 'text': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 1, true);
                  var ptr35 = utf8Encode(e, realloc0, memory0);
                  var len35 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len35, true);
                  dataView(memory0).setUint32(base + 16, ptr35, true);
                  break;
                }
                case 'secret-handle': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 2, true);
                  var ptr36 = utf8Encode(e, realloc0, memory0);
                  var len36 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len36, true);
                  dataView(memory0).setUint32(base + 16, ptr36, true);
                  break;
                }
                case 'integer': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 3, true);
                  dataView(memory0).setBigInt64(base + 16, toInt64(e), true);
                  break;
                }
                case 'decimal': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 4, true);
                  var ptr37 = utf8Encode(e, realloc0, memory0);
                  var len37 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len37, true);
                  dataView(memory0).setUint32(base + 16, ptr37, true);
                  break;
                }
                case 'boolean': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 5, true);
                  dataView(memory0).setInt8(base + 16, e ? 1 : 0, true);
                  break;
                }
                case 'date': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 6, true);
                  var {year: v38_0, month: v38_1, day: v38_2 } = e;
                  dataView(memory0).setInt16(base + 16, toUint16(v38_0), true);
                  dataView(memory0).setInt8(base + 18, toUint8(v38_1), true);
                  dataView(memory0).setInt8(base + 19, toUint8(v38_2), true);
                  break;
                }
                case 'time': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 7, true);
                  var {hour: v39_0, minute: v39_1, second: v39_2 } = e;
                  dataView(memory0).setInt8(base + 16, toUint8(v39_0), true);
                  dataView(memory0).setInt8(base + 17, toUint8(v39_1), true);
                  dataView(memory0).setInt8(base + 18, toUint8(v39_2), true);
                  break;
                }
                case 'time-zone': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 8, true);
                  var ptr40 = utf8Encode(e, realloc0, memory0);
                  var len40 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len40, true);
                  dataView(memory0).setUint32(base + 16, ptr40, true);
                  break;
                }
                case 'choice': {
                  const e = variant42.val;
                  dataView(memory0).setInt8(base + 8, 9, true);
                  var ptr41 = utf8Encode(e, realloc0, memory0);
                  var len41 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len41, true);
                  dataView(memory0).setUint32(base + 16, ptr41, true);
                  break;
                }
                default: {
                  throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant42.tag)}\` (received \`${variant42}\`) specified for \`FieldValue\``);
                }
              }
            }
            dataView(memory0).setUint32(ptr0 + 104, len43, true);
            dataView(memory0).setUint32(ptr0 + 100, result43, true);
            break;
          }
          case 'action': {
            const e = variant60.val;
            dataView(memory0).setInt8(ptr0 + 56, 5, true);
            var {session: v44_0, surface: v44_1, route: v44_2, action: v44_3, fields: v44_4 } = e;
            var ptr45 = utf8Encode(v44_0, realloc0, memory0);
            var len45 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len45, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr45, true);
            var ptr46 = utf8Encode(v44_1, realloc0, memory0);
            var len46 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len46, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr46, true);
            var ptr47 = utf8Encode(v44_2, realloc0, memory0);
            var len47 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 80, len47, true);
            dataView(memory0).setUint32(ptr0 + 76, ptr47, true);
            var ptr48 = utf8Encode(v44_3, realloc0, memory0);
            var len48 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 88, len48, true);
            dataView(memory0).setUint32(ptr0 + 84, ptr48, true);
            var vec59 = v44_4;
            var len59 = vec59.length;
            var result59 = realloc0(0, 0, 8, len59 * 24);
            for (let i = 0; i < vec59.length; i++) {
              const e = vec59[i];
              const base = result59 + i * 24;var {field: v49_0, value: v49_1 } = e;
              var ptr50 = utf8Encode(v49_0, realloc0, memory0);
              var len50 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 4, len50, true);
              dataView(memory0).setUint32(base + 0, ptr50, true);
              var variant58 = v49_1;
              switch (variant58.tag) {
                case 'empty': {
                  dataView(memory0).setInt8(base + 8, 0, true);
                  break;
                }
                case 'text': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 1, true);
                  var ptr51 = utf8Encode(e, realloc0, memory0);
                  var len51 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len51, true);
                  dataView(memory0).setUint32(base + 16, ptr51, true);
                  break;
                }
                case 'secret-handle': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 2, true);
                  var ptr52 = utf8Encode(e, realloc0, memory0);
                  var len52 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len52, true);
                  dataView(memory0).setUint32(base + 16, ptr52, true);
                  break;
                }
                case 'integer': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 3, true);
                  dataView(memory0).setBigInt64(base + 16, toInt64(e), true);
                  break;
                }
                case 'decimal': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 4, true);
                  var ptr53 = utf8Encode(e, realloc0, memory0);
                  var len53 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len53, true);
                  dataView(memory0).setUint32(base + 16, ptr53, true);
                  break;
                }
                case 'boolean': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 5, true);
                  dataView(memory0).setInt8(base + 16, e ? 1 : 0, true);
                  break;
                }
                case 'date': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 6, true);
                  var {year: v54_0, month: v54_1, day: v54_2 } = e;
                  dataView(memory0).setInt16(base + 16, toUint16(v54_0), true);
                  dataView(memory0).setInt8(base + 18, toUint8(v54_1), true);
                  dataView(memory0).setInt8(base + 19, toUint8(v54_2), true);
                  break;
                }
                case 'time': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 7, true);
                  var {hour: v55_0, minute: v55_1, second: v55_2 } = e;
                  dataView(memory0).setInt8(base + 16, toUint8(v55_0), true);
                  dataView(memory0).setInt8(base + 17, toUint8(v55_1), true);
                  dataView(memory0).setInt8(base + 18, toUint8(v55_2), true);
                  break;
                }
                case 'time-zone': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 8, true);
                  var ptr56 = utf8Encode(e, realloc0, memory0);
                  var len56 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len56, true);
                  dataView(memory0).setUint32(base + 16, ptr56, true);
                  break;
                }
                case 'choice': {
                  const e = variant58.val;
                  dataView(memory0).setInt8(base + 8, 9, true);
                  var ptr57 = utf8Encode(e, realloc0, memory0);
                  var len57 = utf8EncodedLen;
                  dataView(memory0).setUint32(base + 20, len57, true);
                  dataView(memory0).setUint32(base + 16, ptr57, true);
                  break;
                }
                default: {
                  throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant58.tag)}\` (received \`${variant58}\`) specified for \`FieldValue\``);
                }
              }
            }
            dataView(memory0).setUint32(ptr0 + 96, len59, true);
            dataView(memory0).setUint32(ptr0 + 92, result59, true);
            break;
          }
          default: {
            throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant60.tag)}\` (received \`${variant60}\`) specified for \`LauncherEvent\``);
          }
        }
        break;
      }
      case 'management': {
        const e = variant95.val;
        dataView(memory0).setInt8(ptr0 + 48, 1, true);
        var variant72 = e;
        switch (variant72.tag) {
          case 'installed': {
            const e = variant72.val;
            dataView(memory0).setInt8(ptr0 + 56, 0, true);
            var {previousVersion: v61_0 } = e;
            var variant63 = v61_0;
            if (variant63 === null || variant63=== undefined) {
              dataView(memory0).setInt8(ptr0 + 64, 0, true);
            } else {
              const e = variant63;
              dataView(memory0).setInt8(ptr0 + 64, 1, true);
              var ptr62 = utf8Encode(e, realloc0, memory0);
              var len62 = utf8EncodedLen;
              dataView(memory0).setUint32(ptr0 + 72, len62, true);
              dataView(memory0).setUint32(ptr0 + 68, ptr62, true);
            }
            break;
          }
          case 'enabled': {
            const e = variant72.val;
            dataView(memory0).setInt8(ptr0 + 56, 1, true);
            var {previousStateRevision: v64_0 } = e;
            dataView(memory0).setBigInt64(ptr0 + 64, toUint64(v64_0), true);
            break;
          }
          case 'disabling': {
            const e = variant72.val;
            dataView(memory0).setInt8(ptr0 + 56, 2, true);
            var {reason: v65_0 } = e;
            var val66 = v65_0;
            let enum66;
            switch (val66) {
              case 'user': {
                enum66 = 0;
                break;
              }
              case 'policy': {
                enum66 = 1;
                break;
              }
              case 'unhealthy': {
                enum66 = 2;
                break;
              }
              case 'update': {
                enum66 = 3;
                break;
              }
              default: {
                if ((v65_0) instanceof Error) {
                  console.error(v65_0);
                }
                
                throw new TypeError(`"${val66}" is not one of the cases of disable-reason`);
              }
            }
            dataView(memory0).setInt8(ptr0 + 64, enum66, true);
            break;
          }
          case 'configured': {
            const e = variant72.val;
            dataView(memory0).setInt8(ptr0 + 56, 3, true);
            var {configRevision: v67_0, changedKeys: v67_1 } = e;
            dataView(memory0).setBigInt64(ptr0 + 64, toUint64(v67_0), true);
            var vec69 = v67_1;
            var len69 = vec69.length;
            var result69 = realloc0(0, 0, 4, len69 * 8);
            for (let i = 0; i < vec69.length; i++) {
              const e = vec69[i];
              const base = result69 + i * 8;var ptr68 = utf8Encode(e, realloc0, memory0);
              var len68 = utf8EncodedLen;
              dataView(memory0).setUint32(base + 4, len68, true);
              dataView(memory0).setUint32(base + 0, ptr68, true);
            }
            dataView(memory0).setUint32(ptr0 + 76, len69, true);
            dataView(memory0).setUint32(ptr0 + 72, result69, true);
            break;
          }
          case 'uninstalling': {
            const e = variant72.val;
            dataView(memory0).setInt8(ptr0 + 56, 4, true);
            var {disposition: v70_0 } = e;
            var val71 = v70_0;
            let enum71;
            switch (val71) {
              case 'delete': {
                enum71 = 0;
                break;
              }
              case 'retain': {
                enum71 = 1;
                break;
              }
              case 'export-then-delete': {
                enum71 = 2;
                break;
              }
              default: {
                if ((v70_0) instanceof Error) {
                  console.error(v70_0);
                }
                
                throw new TypeError(`"${val71}" is not one of the cases of data-disposition`);
              }
            }
            dataView(memory0).setInt8(ptr0 + 64, enum71, true);
            break;
          }
          default: {
            throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant72.tag)}\` (received \`${variant72}\`) specified for \`ManagementEvent\``);
          }
        }
        break;
      }
      case 'service': {
        const e = variant95.val;
        dataView(memory0).setInt8(ptr0 + 48, 2, true);
        var variant86 = e;
        switch (variant86.tag) {
          case 'start': {
            const e = variant86.val;
            dataView(memory0).setInt8(ptr0 + 56, 0, true);
            var {entrypoint: v73_0, instance: v73_1, reason: v73_2 } = e;
            var ptr74 = utf8Encode(v73_0, realloc0, memory0);
            var len74 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len74, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr74, true);
            var ptr75 = utf8Encode(v73_1, realloc0, memory0);
            var len75 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len75, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr75, true);
            var val76 = v73_2;
            let enum76;
            switch (val76) {
              case 'enabled': {
                enum76 = 0;
                break;
              }
              case 'host-restart': {
                enum76 = 1;
                break;
              }
              case 'update': {
                enum76 = 2;
                break;
              }
              case 'manual': {
                enum76 = 3;
                break;
              }
              default: {
                if ((v73_2) instanceof Error) {
                  console.error(v73_2);
                }
                
                throw new TypeError(`"${val76}" is not one of the cases of service-start-reason`);
              }
            }
            dataView(memory0).setInt8(ptr0 + 76, enum76, true);
            break;
          }
          case 'stop': {
            const e = variant86.val;
            dataView(memory0).setInt8(ptr0 + 56, 1, true);
            var {entrypoint: v77_0, instance: v77_1, reason: v77_2 } = e;
            var ptr78 = utf8Encode(v77_0, realloc0, memory0);
            var len78 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len78, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr78, true);
            var ptr79 = utf8Encode(v77_1, realloc0, memory0);
            var len79 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len79, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr79, true);
            var val80 = v77_2;
            let enum80;
            switch (val80) {
              case 'disabled': {
                enum80 = 0;
                break;
              }
              case 'update': {
                enum80 = 1;
                break;
              }
              case 'uninstall': {
                enum80 = 2;
                break;
              }
              case 'unhealthy': {
                enum80 = 3;
                break;
              }
              case 'host-shutdown': {
                enum80 = 4;
                break;
              }
              default: {
                if ((v77_2) instanceof Error) {
                  console.error(v77_2);
                }
                
                throw new TypeError(`"${val80}" is not one of the cases of service-stop-reason`);
              }
            }
            dataView(memory0).setInt8(ptr0 + 76, enum80, true);
            break;
          }
          case 'trigger': {
            const e = variant86.val;
            dataView(memory0).setInt8(ptr0 + 56, 2, true);
            var {entrypoint: v81_0, instance: v81_1, triggerId: v81_2, payload: v81_3 } = e;
            var ptr82 = utf8Encode(v81_0, realloc0, memory0);
            var len82 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 64, len82, true);
            dataView(memory0).setUint32(ptr0 + 60, ptr82, true);
            var ptr83 = utf8Encode(v81_1, realloc0, memory0);
            var len83 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 72, len83, true);
            dataView(memory0).setUint32(ptr0 + 68, ptr83, true);
            var ptr84 = utf8Encode(v81_2, realloc0, memory0);
            var len84 = utf8EncodedLen;
            dataView(memory0).setUint32(ptr0 + 80, len84, true);
            dataView(memory0).setUint32(ptr0 + 76, ptr84, true);
            var val85 = v81_3;
            var len85 = val85.byteLength;
            var ptr85 = realloc0(0, 0, 1, len85 * 1);
            var src85 = new Uint8Array(val85.buffer || val85, val85.byteOffset, len85 * 1);
            (new Uint8Array(memory0.buffer, ptr85, len85 * 1)).set(src85);
            dataView(memory0).setUint32(ptr0 + 88, len85, true);
            dataView(memory0).setUint32(ptr0 + 84, ptr85, true);
            break;
          }
          default: {
            throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant86.tag)}\` (received \`${variant86}\`) specified for \`ServiceEvent\``);
          }
        }
        break;
      }
      case 'scheduled': {
        const e = variant95.val;
        dataView(memory0).setInt8(ptr0 + 48, 3, true);
        var {schedule: v87_0, key: v87_1, occurrence: v87_2, scheduledForUtc: v87_3, dispatchedAtUtc: v87_4, classification: v87_5, attempt: v87_6, payload: v87_7 } = e;
        var ptr88 = utf8Encode(v87_0, realloc0, memory0);
        var len88 = utf8EncodedLen;
        dataView(memory0).setUint32(ptr0 + 60, len88, true);
        dataView(memory0).setUint32(ptr0 + 56, ptr88, true);
        var ptr89 = utf8Encode(v87_1, realloc0, memory0);
        var len89 = utf8EncodedLen;
        dataView(memory0).setUint32(ptr0 + 68, len89, true);
        dataView(memory0).setUint32(ptr0 + 64, ptr89, true);
        var ptr90 = utf8Encode(v87_2, realloc0, memory0);
        var len90 = utf8EncodedLen;
        dataView(memory0).setUint32(ptr0 + 76, len90, true);
        dataView(memory0).setUint32(ptr0 + 72, ptr90, true);
        var ptr91 = utf8Encode(v87_3, realloc0, memory0);
        var len91 = utf8EncodedLen;
        dataView(memory0).setUint32(ptr0 + 84, len91, true);
        dataView(memory0).setUint32(ptr0 + 80, ptr91, true);
        var ptr92 = utf8Encode(v87_4, realloc0, memory0);
        var len92 = utf8EncodedLen;
        dataView(memory0).setUint32(ptr0 + 92, len92, true);
        dataView(memory0).setUint32(ptr0 + 88, ptr92, true);
        var val93 = v87_5;
        let enum93;
        switch (val93) {
          case 'on-time': {
            enum93 = 0;
            break;
          }
          case 'catch-up': {
            enum93 = 1;
            break;
          }
          case 'missed': {
            enum93 = 2;
            break;
          }
          case 'permission-denied': {
            enum93 = 3;
            break;
          }
          default: {
            if ((v87_5) instanceof Error) {
              console.error(v87_5);
            }
            
            throw new TypeError(`"${val93}" is not one of the cases of delivery-classification`);
          }
        }
        dataView(memory0).setInt8(ptr0 + 96, enum93, true);
        dataView(memory0).setInt32(ptr0 + 100, toUint32(v87_6), true);
        var val94 = v87_7;
        var len94 = val94.byteLength;
        var ptr94 = realloc0(0, 0, 1, len94 * 1);
        var src94 = new Uint8Array(val94.buffer || val94, val94.byteOffset, len94 * 1);
        (new Uint8Array(memory0.buffer, ptr94, len94 * 1)).set(src94);
        dataView(memory0).setUint32(ptr0 + 108, len94, true);
        dataView(memory0).setUint32(ptr0 + 104, ptr94, true);
        break;
      }
      default: {
        throw new TypeError(`invalid variant tag value \`${JSON.stringify(variant95.tag)}\` (received \`${variant95}\`) specified for \`AppEvent\``);
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="handle-event"][Instruction::CallWasm] enter', {
      funcName: 'handle-event',
      paramCount: 1,
      async: false,
      postReturn: true,
    });
    const _wasm_call_currentTaskID = startCurrentTask(0, false, 'guest001HandleEvent');
    const ret = guest001HandleEvent(ptr0);
    endCurrentTask(0);
    let variant144;
    switch (dataView(memory0).getUint8(ret + 0, true)) {
      case 0: {
        var len138 = dataView(memory0).getUint32(ret + 8, true);
        var base138 = dataView(memory0).getUint32(ret + 4, true);
        var result138 = [];
        for (let i = 0; i < len138; i++) {
          const base = base138 + i * 48;
          var ptr96 = dataView(memory0).getUint32(base + 0, true);
          var len96 = dataView(memory0).getUint32(base + 4, true);
          var result96 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr96, len96));
          var ptr97 = dataView(memory0).getUint32(base + 8, true);
          var len97 = dataView(memory0).getUint32(base + 12, true);
          var result97 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr97, len97));
          var ptr98 = dataView(memory0).getUint32(base + 16, true);
          var len98 = dataView(memory0).getUint32(base + 20, true);
          var result98 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr98, len98));
          var ptr99 = dataView(memory0).getUint32(base + 24, true);
          var len99 = dataView(memory0).getUint32(base + 28, true);
          var result99 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr99, len99));
          var ptr100 = dataView(memory0).getUint32(base + 32, true);
          var len100 = dataView(memory0).getUint32(base + 36, true);
          var result100 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr100, len100));
          var len137 = dataView(memory0).getUint32(base + 44, true);
          var base137 = dataView(memory0).getUint32(base + 40, true);
          var result137 = [];
          for (let i = 0; i < len137; i++) {
            const base = base137 + i * 96;
            var ptr101 = dataView(memory0).getUint32(base + 0, true);
            var len101 = dataView(memory0).getUint32(base + 4, true);
            var result101 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr101, len101));
            let variant103;
            switch (dataView(memory0).getUint8(base + 8, true)) {
              case 0: {
                variant103 = undefined;
                break;
              }
              case 1: {
                var ptr102 = dataView(memory0).getUint32(base + 12, true);
                var len102 = dataView(memory0).getUint32(base + 16, true);
                var result102 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr102, len102));
                variant103 = result102;
                break;
              }
              default: {
                throw new TypeError('invalid variant discriminant for option');
              }
            }
            let variant136;
            switch (dataView(memory0).getUint8(base + 24, true)) {
              case 0: {
                var ptr104 = dataView(memory0).getUint32(base + 32, true);
                var len104 = dataView(memory0).getUint32(base + 36, true);
                var result104 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr104, len104));
                let enum105;
                switch (dataView(memory0).getUint8(base + 40, true)) {
                  case 0: {
                    enum105 = 'body';
                    break;
                  }
                  case 1: {
                    enum105 = 'caption';
                    break;
                  }
                  case 2: {
                    enum105 = 'title';
                    break;
                  }
                  case 3: {
                    enum105 = 'heading';
                    break;
                  }
                  case 4: {
                    enum105 = 'status';
                    break;
                  }
                  default: {
                    throw new TypeError('invalid discriminant specified for TextStyle');
                  }
                }
                variant136= {
                  tag: 'text',
                  val: {
                    text: result104,
                    style: enum105,
                  }
                };
                break;
              }
              case 1: {
                var ptr106 = dataView(memory0).getUint32(base + 32, true);
                var len106 = dataView(memory0).getUint32(base + 36, true);
                var result106 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr106, len106));
                var ptr107 = dataView(memory0).getUint32(base + 40, true);
                var len107 = dataView(memory0).getUint32(base + 44, true);
                var result107 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr107, len107));
                let enum108;
                switch (dataView(memory0).getUint8(base + 48, true)) {
                  case 0: {
                    enum108 = 'primary';
                    break;
                  }
                  case 1: {
                    enum108 = 'secondary';
                    break;
                  }
                  case 2: {
                    enum108 = 'destructive';
                    break;
                  }
                  case 3: {
                    enum108 = 'quiet';
                    break;
                  }
                  default: {
                    throw new TypeError('invalid discriminant specified for ButtonStyle');
                  }
                }
                var bool109 = dataView(memory0).getUint8(base + 49, true);
                variant136= {
                  tag: 'button',
                  val: {
                    label: result106,
                    action: result107,
                    style: enum108,
                    disabled: bool109 == 0 ? false : (bool109 == 1 ? true : throwInvalidBool()),
                  }
                };
                break;
              }
              case 2: {
                var ptr110 = dataView(memory0).getUint32(base + 32, true);
                var len110 = dataView(memory0).getUint32(base + 36, true);
                var result110 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr110, len110));
                var ptr111 = dataView(memory0).getUint32(base + 40, true);
                var len111 = dataView(memory0).getUint32(base + 44, true);
                var result111 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr111, len111));
                let enum112;
                switch (dataView(memory0).getUint8(base + 48, true)) {
                  case 0: {
                    enum112 = 'text';
                    break;
                  }
                  case 1: {
                    enum112 = 'integer';
                    break;
                  }
                  case 2: {
                    enum112 = 'decimal';
                    break;
                  }
                  case 3: {
                    enum112 = 'boolean';
                    break;
                  }
                  case 4: {
                    enum112 = 'date';
                    break;
                  }
                  case 5: {
                    enum112 = 'time';
                    break;
                  }
                  case 6: {
                    enum112 = 'time-zone';
                    break;
                  }
                  case 7: {
                    enum112 = 'choice';
                    break;
                  }
                  default: {
                    throw new TypeError('invalid discriminant specified for FieldKind');
                  }
                }
                let variant119;
                switch (dataView(memory0).getUint8(base + 56, true)) {
                  case 0: {
                    variant119= {
                      tag: 'empty',
                    };
                    break;
                  }
                  case 1: {
                    var ptr113 = dataView(memory0).getUint32(base + 64, true);
                    var len113 = dataView(memory0).getUint32(base + 68, true);
                    var result113 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr113, len113));
                    variant119= {
                      tag: 'text',
                      val: result113
                    };
                    break;
                  }
                  case 2: {
                    var ptr114 = dataView(memory0).getUint32(base + 64, true);
                    var len114 = dataView(memory0).getUint32(base + 68, true);
                    var result114 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr114, len114));
                    variant119= {
                      tag: 'secret-handle',
                      val: result114
                    };
                    break;
                  }
                  case 3: {
                    variant119= {
                      tag: 'integer',
                      val: dataView(memory0).getBigInt64(base + 64, true)
                    };
                    break;
                  }
                  case 4: {
                    var ptr115 = dataView(memory0).getUint32(base + 64, true);
                    var len115 = dataView(memory0).getUint32(base + 68, true);
                    var result115 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr115, len115));
                    variant119= {
                      tag: 'decimal',
                      val: result115
                    };
                    break;
                  }
                  case 5: {
                    var bool116 = dataView(memory0).getUint8(base + 64, true);
                    variant119= {
                      tag: 'boolean',
                      val: bool116 == 0 ? false : (bool116 == 1 ? true : throwInvalidBool())
                    };
                    break;
                  }
                  case 6: {
                    variant119= {
                      tag: 'date',
                      val: {
                        year: clampGuest(dataView(memory0).getUint16(base + 64, true), 0, 65535),
                        month: clampGuest(dataView(memory0).getUint8(base + 66, true), 0, 255),
                        day: clampGuest(dataView(memory0).getUint8(base + 67, true), 0, 255),
                      }
                    };
                    break;
                  }
                  case 7: {
                    variant119= {
                      tag: 'time',
                      val: {
                        hour: clampGuest(dataView(memory0).getUint8(base + 64, true), 0, 255),
                        minute: clampGuest(dataView(memory0).getUint8(base + 65, true), 0, 255),
                        second: clampGuest(dataView(memory0).getUint8(base + 66, true), 0, 255),
                      }
                    };
                    break;
                  }
                  case 8: {
                    var ptr117 = dataView(memory0).getUint32(base + 64, true);
                    var len117 = dataView(memory0).getUint32(base + 68, true);
                    var result117 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr117, len117));
                    variant119= {
                      tag: 'time-zone',
                      val: result117
                    };
                    break;
                  }
                  case 9: {
                    var ptr118 = dataView(memory0).getUint32(base + 64, true);
                    var len118 = dataView(memory0).getUint32(base + 68, true);
                    var result118 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr118, len118));
                    variant119= {
                      tag: 'choice',
                      val: result118
                    };
                    break;
                  }
                  default: {
                    throw new TypeError('invalid variant discriminant for FieldValue');
                  }
                }
                var bool120 = dataView(memory0).getUint8(base + 72, true);
                var bool121 = dataView(memory0).getUint8(base + 73, true);
                var len124 = dataView(memory0).getUint32(base + 80, true);
                var base124 = dataView(memory0).getUint32(base + 76, true);
                var result124 = [];
                for (let i = 0; i < len124; i++) {
                  const base = base124 + i * 16;
                  var ptr122 = dataView(memory0).getUint32(base + 0, true);
                  var len122 = dataView(memory0).getUint32(base + 4, true);
                  var result122 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr122, len122));
                  var ptr123 = dataView(memory0).getUint32(base + 8, true);
                  var len123 = dataView(memory0).getUint32(base + 12, true);
                  var result123 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr123, len123));
                  result124.push({
                    value: result122,
                    label: result123,
                  });
                }
                let variant126;
                switch (dataView(memory0).getUint8(base + 84, true)) {
                  case 0: {
                    variant126 = undefined;
                    break;
                  }
                  case 1: {
                    var ptr125 = dataView(memory0).getUint32(base + 88, true);
                    var len125 = dataView(memory0).getUint32(base + 92, true);
                    var result125 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr125, len125));
                    variant126 = result125;
                    break;
                  }
                  default: {
                    throw new TypeError('invalid variant discriminant for option');
                  }
                }
                variant136= {
                  tag: 'field',
                  val: {
                    field: result110,
                    label: result111,
                    kind: enum112,
                    value: variant119,
                    required: bool120 == 0 ? false : (bool120 == 1 ? true : throwInvalidBool()),
                    sensitive: bool121 == 0 ? false : (bool121 == 1 ? true : throwInvalidBool()),
                    choices: result124,
                    validationMessage: variant126,
                  }
                };
                break;
              }
              case 3: {
                let variant128;
                switch (dataView(memory0).getUint8(base + 32, true)) {
                  case 0: {
                    variant128 = undefined;
                    break;
                  }
                  case 1: {
                    var ptr127 = dataView(memory0).getUint32(base + 36, true);
                    var len127 = dataView(memory0).getUint32(base + 40, true);
                    var result127 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr127, len127));
                    variant128 = result127;
                    break;
                  }
                  default: {
                    throw new TypeError('invalid variant discriminant for option');
                  }
                }
                variant136= {
                  tag: 'list-container',
                  val: {
                    label: variant128,
                  }
                };
                break;
              }
              case 4: {
                var ptr129 = dataView(memory0).getUint32(base + 32, true);
                var len129 = dataView(memory0).getUint32(base + 36, true);
                var result129 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr129, len129));
                let variant130;
                switch (dataView(memory0).getUint8(base + 44, true)) {
                  case 0: {
                    variant130 = undefined;
                    break;
                  }
                  case 1: {
                    variant130 = dataView(memory0).getInt32(base + 48, true) >>> 0;
                    break;
                  }
                  default: {
                    throw new TypeError('invalid variant discriminant for option');
                  }
                }
                variant136= {
                  tag: 'progress',
                  val: {
                    label: result129,
                    current: dataView(memory0).getInt32(base + 40, true) >>> 0,
                    total: variant130,
                  }
                };
                break;
              }
              case 5: {
                var ptr131 = dataView(memory0).getUint32(base + 32, true);
                var len131 = dataView(memory0).getUint32(base + 36, true);
                var result131 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr131, len131));
                var ptr132 = dataView(memory0).getUint32(base + 40, true);
                var len132 = dataView(memory0).getUint32(base + 44, true);
                var result132 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr132, len132));
                var ptr133 = dataView(memory0).getUint32(base + 48, true);
                var len133 = dataView(memory0).getUint32(base + 52, true);
                var result133 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr133, len133));
                var ptr134 = dataView(memory0).getUint32(base + 56, true);
                var len134 = dataView(memory0).getUint32(base + 60, true);
                var result134 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr134, len134));
                var bool135 = dataView(memory0).getUint8(base + 64, true);
                variant136= {
                  tag: 'confirmation',
                  val: {
                    title: result131,
                    message: result132,
                    confirmAction: result133,
                    cancelAction: result134,
                    destructive: bool135 == 0 ? false : (bool135 == 1 ? true : throwInvalidBool()),
                  }
                };
                break;
              }
              default: {
                throw new TypeError('invalid variant discriminant for NodeKind');
              }
            }
            result137.push({
              id: result101,
              parent: variant103,
              kind: variant136,
            });
          }
          result138.push({
            session: result96,
            surface: result97,
            route: result98,
            view: {
              title: result99,
              root: result100,
              nodes: result137,
            },
          });
        }
        let variant140;
        switch (dataView(memory0).getUint8(ret + 12, true)) {
          case 0: {
            variant140 = undefined;
            break;
          }
          case 1: {
            var ptr139 = dataView(memory0).getUint32(ret + 16, true);
            var len139 = dataView(memory0).getUint32(ret + 20, true);
            var result139 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr139, len139));
            variant140 = result139;
            break;
          }
          default: {
            throw new TypeError('invalid variant discriminant for option');
          }
        }
        variant144= {
          tag: 'ok',
          val: {
            surfaces: result138,
            diagnostic: variant140,
          }
        };
        break;
      }
      case 1: {
        let enum141;
        switch (dataView(memory0).getUint8(ret + 4, true)) {
          case 0: {
            enum141 = 'invalid-argument';
            break;
          }
          case 1: {
            enum141 = 'incompatible-contract';
            break;
          }
          case 2: {
            enum141 = 'unsupported-version';
            break;
          }
          case 3: {
            enum141 = 'unknown-interface';
            break;
          }
          case 4: {
            enum141 = 'missing-interface';
            break;
          }
          case 5: {
            enum141 = 'permission-denied';
            break;
          }
          case 6: {
            enum141 = 'consent-required';
            break;
          }
          case 7: {
            enum141 = 'capability-unavailable';
            break;
          }
          case 8: {
            enum141 = 'unsupported-surface';
            break;
          }
          case 9: {
            enum141 = 'resource-limit';
            break;
          }
          case 10: {
            enum141 = 'deadline-exceeded';
            break;
          }
          case 11: {
            enum141 = 'cancelled';
            break;
          }
          case 12: {
            enum141 = 'app-disabled';
            break;
          }
          case 13: {
            enum141 = 'app-uninstalled';
            break;
          }
          case 14: {
            enum141 = 'upgrade-in-progress';
            break;
          }
          case 15: {
            enum141 = 'integrity-failure';
            break;
          }
          case 16: {
            enum141 = 'not-found';
            break;
          }
          case 17: {
            enum141 = 'conflict';
            break;
          }
          case 18: {
            enum141 = 'stale-revision';
            break;
          }
          case 19: {
            enum141 = 'malformed-output';
            break;
          }
          case 20: {
            enum141 = 'forged-identifier';
            break;
          }
          case 21: {
            enum141 = 'internal';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for ErrorCode');
          }
        }
        var ptr142 = dataView(memory0).getUint32(ret + 8, true);
        var len142 = dataView(memory0).getUint32(ret + 12, true);
        var result142 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr142, len142));
        var bool143 = dataView(memory0).getUint8(ret + 16, true);
        variant144= {
          tag: 'err',
          val: {
            code: enum141,
            message: result142,
            retryable: bool143 == 0 ? false : (bool143 == 1 ? true : throwInvalidBool()),
          }
        };
        break;
      }
      default: {
        throw new TypeError('invalid variant discriminant for expected');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="handle-event"][Instruction::Return]', {
      funcName: 'handle-event',
      paramCount: 1,
      async: false,
      postReturn: true
    });
    const retCopy = variant144;
    
    let cstate = getOrCreateAsyncState(0);
    cstate.mayLeave = false;
    postReturn1(ret);
    cstate.mayLeave = true;
    
    
    
    if (typeof retCopy === 'object' && retCopy.tag === 'err') {
      throw new ComponentError(retCopy.val);
    }
    return retCopy.val;
    
  }
  let guest001Health;
  
  function health(arg0, arg1) {
    var {eventId: v0_0, idempotencyKey: v0_1, cancellation: v0_2, generation: v0_3, profile: v0_4, deadlineMonotonicMs: v0_5 } = arg0;
    var ptr1 = utf8Encode(v0_0, realloc0, memory0);
    var len1 = utf8EncodedLen;
    var ptr2 = utf8Encode(v0_1, realloc0, memory0);
    var len2 = utf8EncodedLen;
    var ptr3 = utf8Encode(v0_2, realloc0, memory0);
    var len3 = utf8EncodedLen;
    var ptr4 = utf8Encode(v0_3, realloc0, memory0);
    var len4 = utf8EncodedLen;
    var val5 = v0_4;
    let enum5;
    switch (val5) {
      case 'desktop': {
        enum5 = 0;
        break;
      }
      case 'web-preview': {
        enum5 = 1;
        break;
      }
      case 'web-runtime': {
        enum5 = 2;
        break;
      }
      case 'headless': {
        enum5 = 3;
        break;
      }
      default: {
        if ((v0_4) instanceof Error) {
          console.error(v0_4);
        }
        
        throw new TypeError(`"${val5}" is not one of the cases of execution-profile`);
      }
    }
    var {serviceEntrypoint: v6_0, includeDependencies: v6_1 } = arg1;
    var variant8 = v6_0;
    let variant8_0;
    let variant8_1;
    let variant8_2;
    if (variant8 === null || variant8=== undefined) {
      variant8_0 = 0;
      variant8_1 = 0;
      variant8_2 = 0;
    } else {
      const e = variant8;
      var ptr7 = utf8Encode(e, realloc0, memory0);
      var len7 = utf8EncodedLen;
      variant8_0 = 1;
      variant8_1 = ptr7;
      variant8_2 = len7;
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="health"][Instruction::CallWasm] enter', {
      funcName: 'health',
      paramCount: 14,
      async: false,
      postReturn: true,
    });
    const _wasm_call_currentTaskID = startCurrentTask(0, false, 'guest001Health');
    const ret = guest001Health(ptr1, len1, ptr2, len2, ptr3, len3, ptr4, len4, enum5, toUint64(v0_5), variant8_0, variant8_1, variant8_2, v6_1 ? 1 : 0);
    endCurrentTask(0);
    let variant17;
    switch (dataView(memory0).getUint8(ret + 0, true)) {
      case 0: {
        let enum9;
        switch (dataView(memory0).getUint8(ret + 4, true)) {
          case 0: {
            enum9 = 'healthy';
            break;
          }
          case 1: {
            enum9 = 'degraded';
            break;
          }
          case 2: {
            enum9 = 'unhealthy';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for HealthStatus');
          }
        }
        var len13 = dataView(memory0).getUint32(ret + 12, true);
        var base13 = dataView(memory0).getUint32(ret + 8, true);
        var result13 = [];
        for (let i = 0; i < len13; i++) {
          const base = base13 + i * 20;
          var ptr10 = dataView(memory0).getUint32(base + 0, true);
          var len10 = dataView(memory0).getUint32(base + 4, true);
          var result10 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr10, len10));
          let enum11;
          switch (dataView(memory0).getUint8(base + 8, true)) {
            case 0: {
              enum11 = 'healthy';
              break;
            }
            case 1: {
              enum11 = 'degraded';
              break;
            }
            case 2: {
              enum11 = 'unhealthy';
              break;
            }
            default: {
              throw new TypeError('invalid discriminant specified for HealthStatus');
            }
          }
          var ptr12 = dataView(memory0).getUint32(base + 12, true);
          var len12 = dataView(memory0).getUint32(base + 16, true);
          var result12 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr12, len12));
          result13.push({
            name: result10,
            status: enum11,
            message: result12,
          });
        }
        variant17= {
          tag: 'ok',
          val: {
            status: enum9,
            checks: result13,
          }
        };
        break;
      }
      case 1: {
        let enum14;
        switch (dataView(memory0).getUint8(ret + 4, true)) {
          case 0: {
            enum14 = 'invalid-argument';
            break;
          }
          case 1: {
            enum14 = 'incompatible-contract';
            break;
          }
          case 2: {
            enum14 = 'unsupported-version';
            break;
          }
          case 3: {
            enum14 = 'unknown-interface';
            break;
          }
          case 4: {
            enum14 = 'missing-interface';
            break;
          }
          case 5: {
            enum14 = 'permission-denied';
            break;
          }
          case 6: {
            enum14 = 'consent-required';
            break;
          }
          case 7: {
            enum14 = 'capability-unavailable';
            break;
          }
          case 8: {
            enum14 = 'unsupported-surface';
            break;
          }
          case 9: {
            enum14 = 'resource-limit';
            break;
          }
          case 10: {
            enum14 = 'deadline-exceeded';
            break;
          }
          case 11: {
            enum14 = 'cancelled';
            break;
          }
          case 12: {
            enum14 = 'app-disabled';
            break;
          }
          case 13: {
            enum14 = 'app-uninstalled';
            break;
          }
          case 14: {
            enum14 = 'upgrade-in-progress';
            break;
          }
          case 15: {
            enum14 = 'integrity-failure';
            break;
          }
          case 16: {
            enum14 = 'not-found';
            break;
          }
          case 17: {
            enum14 = 'conflict';
            break;
          }
          case 18: {
            enum14 = 'stale-revision';
            break;
          }
          case 19: {
            enum14 = 'malformed-output';
            break;
          }
          case 20: {
            enum14 = 'forged-identifier';
            break;
          }
          case 21: {
            enum14 = 'internal';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for ErrorCode');
          }
        }
        var ptr15 = dataView(memory0).getUint32(ret + 8, true);
        var len15 = dataView(memory0).getUint32(ret + 12, true);
        var result15 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr15, len15));
        var bool16 = dataView(memory0).getUint8(ret + 16, true);
        variant17= {
          tag: 'err',
          val: {
            code: enum14,
            message: result15,
            retryable: bool16 == 0 ? false : (bool16 == 1 ? true : throwInvalidBool()),
          }
        };
        break;
      }
      default: {
        throw new TypeError('invalid variant discriminant for expected');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="health"][Instruction::Return]', {
      funcName: 'health',
      paramCount: 1,
      async: false,
      postReturn: true
    });
    const retCopy = variant17;
    
    let cstate = getOrCreateAsyncState(0);
    cstate.mayLeave = false;
    postReturn0(ret);
    cstate.mayLeave = true;
    
    
    
    if (typeof retCopy === 'object' && retCopy.tag === 'err') {
      throw new ComponentError(retCopy.val);
    }
    return retCopy.val;
    
  }
  let guest001Migrate;
  
  function migrate(arg0, arg1) {
    var {eventId: v0_0, idempotencyKey: v0_1, cancellation: v0_2, generation: v0_3, profile: v0_4, deadlineMonotonicMs: v0_5 } = arg0;
    var ptr1 = utf8Encode(v0_0, realloc0, memory0);
    var len1 = utf8EncodedLen;
    var ptr2 = utf8Encode(v0_1, realloc0, memory0);
    var len2 = utf8EncodedLen;
    var ptr3 = utf8Encode(v0_2, realloc0, memory0);
    var len3 = utf8EncodedLen;
    var ptr4 = utf8Encode(v0_3, realloc0, memory0);
    var len4 = utf8EncodedLen;
    var val5 = v0_4;
    let enum5;
    switch (val5) {
      case 'desktop': {
        enum5 = 0;
        break;
      }
      case 'web-preview': {
        enum5 = 1;
        break;
      }
      case 'web-runtime': {
        enum5 = 2;
        break;
      }
      case 'headless': {
        enum5 = 3;
        break;
      }
      default: {
        if ((v0_4) instanceof Error) {
          console.error(v0_4);
        }
        
        throw new TypeError(`"${val5}" is not one of the cases of execution-profile`);
      }
    }
    var {fromSchema: v6_0, toSchema: v6_1, sourceStateRevision: v6_2, targetStateRevision: v6_3 } = arg1;
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="migrate"][Instruction::CallWasm] enter', {
      funcName: 'migrate',
      paramCount: 14,
      async: false,
      postReturn: true,
    });
    const _wasm_call_currentTaskID = startCurrentTask(0, false, 'guest001Migrate');
    const ret = guest001Migrate(ptr1, len1, ptr2, len2, ptr3, len3, ptr4, len4, enum5, toUint64(v0_5), toUint32(v6_0), toUint32(v6_1), toUint64(v6_2), toUint64(v6_3));
    endCurrentTask(0);
    let variant11;
    switch (dataView(memory0).getUint8(ret + 0, true)) {
      case 0: {
        let enum7;
        switch (dataView(memory0).getUint8(ret + 8, true)) {
          case 0: {
            enum7 = 'unchanged';
            break;
          }
          case 1: {
            enum7 = 'migrated';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for MigrationStatus');
          }
        }
        variant11= {
          tag: 'ok',
          val: {
            status: enum7,
            targetStateRevision: BigInt.asUintN(64, dataView(memory0).getBigInt64(ret + 16, true)),
          }
        };
        break;
      }
      case 1: {
        let enum8;
        switch (dataView(memory0).getUint8(ret + 8, true)) {
          case 0: {
            enum8 = 'invalid-argument';
            break;
          }
          case 1: {
            enum8 = 'incompatible-contract';
            break;
          }
          case 2: {
            enum8 = 'unsupported-version';
            break;
          }
          case 3: {
            enum8 = 'unknown-interface';
            break;
          }
          case 4: {
            enum8 = 'missing-interface';
            break;
          }
          case 5: {
            enum8 = 'permission-denied';
            break;
          }
          case 6: {
            enum8 = 'consent-required';
            break;
          }
          case 7: {
            enum8 = 'capability-unavailable';
            break;
          }
          case 8: {
            enum8 = 'unsupported-surface';
            break;
          }
          case 9: {
            enum8 = 'resource-limit';
            break;
          }
          case 10: {
            enum8 = 'deadline-exceeded';
            break;
          }
          case 11: {
            enum8 = 'cancelled';
            break;
          }
          case 12: {
            enum8 = 'app-disabled';
            break;
          }
          case 13: {
            enum8 = 'app-uninstalled';
            break;
          }
          case 14: {
            enum8 = 'upgrade-in-progress';
            break;
          }
          case 15: {
            enum8 = 'integrity-failure';
            break;
          }
          case 16: {
            enum8 = 'not-found';
            break;
          }
          case 17: {
            enum8 = 'conflict';
            break;
          }
          case 18: {
            enum8 = 'stale-revision';
            break;
          }
          case 19: {
            enum8 = 'malformed-output';
            break;
          }
          case 20: {
            enum8 = 'forged-identifier';
            break;
          }
          case 21: {
            enum8 = 'internal';
            break;
          }
          default: {
            throw new TypeError('invalid discriminant specified for ErrorCode');
          }
        }
        var ptr9 = dataView(memory0).getUint32(ret + 12, true);
        var len9 = dataView(memory0).getUint32(ret + 16, true);
        var result9 = utf8Decoder.decode(new Uint8Array(memory0.buffer, ptr9, len9));
        var bool10 = dataView(memory0).getUint8(ret + 20, true);
        variant11= {
          tag: 'err',
          val: {
            code: enum8,
            message: result9,
            retryable: bool10 == 0 ? false : (bool10 == 1 ? true : throwInvalidBool()),
          }
        };
        break;
      }
      default: {
        throw new TypeError('invalid variant discriminant for expected');
      }
    }
    _debugLog('[iface="vibapp:experimental-v0/guest@0.0.1", function="migrate"][Instruction::Return]', {
      funcName: 'migrate',
      paramCount: 1,
      async: false,
      postReturn: true
    });
    const retCopy = variant11;
    
    let cstate = getOrCreateAsyncState(0);
    cstate.mayLeave = false;
    postReturn2(ret);
    cstate.mayLeave = true;
    
    
    
    if (typeof retCopy === 'object' && retCopy.tag === 'err') {
      throw new ComponentError(retCopy.val);
    }
    return retCopy.val;
    
  }
  guest001Describe = exports1['vibapp:experimental-v0/guest@0.0.1#describe'];
  guest001GetSettingsSchema = exports1['vibapp:experimental-v0/guest@0.0.1#get-settings-schema'];
  guest001ValidateSettings = exports1['vibapp:experimental-v0/guest@0.0.1#validate-settings'];
  guest001HandleEvent = exports1['vibapp:experimental-v0/guest@0.0.1#handle-event'];
  guest001Health = exports1['vibapp:experimental-v0/guest@0.0.1#health'];
  guest001Migrate = exports1['vibapp:experimental-v0/guest@0.0.1#migrate'];
  const guest001 = {
    describe: describe,
    getSettingsSchema: getSettingsSchema,
    handleEvent: handleEvent,
    health: health,
    migrate: migrate,
    validateSettings: validateSettings,
    
  };
  
  return { guest: guest001, 'vibapp:experimental-v0/guest@0.0.1': guest001,  };
})();
let promise, resolve, reject;
function runNext (value) {
  try {
    let done;
    do {
      ({ value, done } = gen.next(value));
    } while (!(value instanceof Promise) && !done);
    if (done) {
      if (resolve) return resolve(value);
      else return value;
    }
    if (!promise) promise = new Promise((_resolve, _reject) => (resolve = _resolve, reject = _reject));
    value.then(nextVal => done ? resolve() : runNext(nextVal), reject);
  }
  catch (e) {
    if (reject) reject(e);
    else throw e;
  }
}
const maybeSyncReturn = runNext(null);
return promise || maybeSyncReturn;
}

import * as host from './host-adapter-5f1edc6f4ff369d10974f7000c3e0204060708f1d3db90790ad049738c00a388.mjs';
const coreBytes = {"app.core.wasm":"AGFzbQEAAAABjgERYAN/f38Bf2ACf38Bf2ABfwBgAAF+YAR/f39/AGAIf39/f39/f38AYAAAYAJ/fwBgDn9/f39/f39/f35/f39/AX9gAAF/YAF/AX9gDn9/f39/f39/f35+fn9/AX9gA39/fwBgBH9/f38Bf2AOf39/f39/f39/fn9/fn4Bf2AFf39/f38AYAZ/f39/f38AAqACBiJ2aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2Nsb2NrQDAuMC4xCHdhbGwtbm93AAIidmliYXBwOmV4cGVyaW1lbnRhbC12MC9jbG9ja0AwLjAuMQ1tb25vdG9uaWMtbm93AAMfdmliYXBwOmV4cGVyaW1lbnRhbC12MC9rdkAwLjAuMQtzY2FuLXByZWZpeAAEIHZpYmFwcDpleHBlcmltZW50YWwtdjAvbG9nQDAuMC4xBXdyaXRlAAUmdmliYXBwOmV4cGVyaW1lbnRhbC12MC9ob3N0LWluZm9AMC4wLjENZGVzY3JpYmUtaG9zdAACJXZpYmFwcDpleHBlcmltZW50YWwtdjAvc2V0dGluZ3NAMC4wLjEHY3VycmVudAACA0A/BgcIAgkKCwEBDA0CDAwCBgECBgICAg0ACQkKCA4LAg8CAhAPAgYGDAIHAAIBAQwABwQMBgcNAQABAQwBAAEPBAUBcAEMDAUDAQARBiAFfwFBgIDAAAt/AEEAC38AQbCFwAALfwBBAwt/AEEBCwfSBRAGbWVtb3J5AgAfX192aWJhcHBfZm9yY2VfZGVjbGFyZWRfaW1wb3J0cwAYNWNhYmlfcG9zdF92aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI2Rlc2NyaWJlABk5Y2FiaV9wb3N0X3ZpYmFwcDpleHBlcmltZW50YWwtdjAvZ3Vlc3RAMC4wLjEjaGFuZGxlLWV2ZW50ABo0Y2FiaV9wb3N0X3ZpYmFwcDpleHBlcmltZW50YWwtdjAvZ3Vlc3RAMC4wLjEjbWlncmF0ZQAbDGNhYmlfcmVhbGxvYwAcBm1lbWNtcAAdK3ZpYmFwcDpleHBlcmltZW50YWwtdjAvZ3Vlc3RAMC4wLjEjZGVzY3JpYmUAHjZ2aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI2dldC1zZXR0aW5ncy1zY2hlbWEA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NwMAIABBB2ogFDoAACAAQQZqIB06AAAgAEEFaiAfOgAAIABBBGogFToAACABIBNBAWoiEzYCcCASQRhqIRIgGUEgaiEZIBcgE0cNAAsgASgCaCEbCyAYQQh2IQAgFkEIdiEVICFCIIYgIK2EISIgG61CIIYgIYQhHCAMrUIghiANrYQhISAOrUIghiAPrYQhIyAQrUIghiARrYQhJCABKQJsISUgDiESIAwhDgwECyAVIBMQtoCAgAAACyAALQBUQQBHIRYgDa1CIIYgDK2EISEgD61CIIYgDq2EISMgEa1CIIYgEK2EISQgECESIAwhIEGDgICAeCEQDAILIA2tQiCGIAythCEhIA+tQiCGIA6thCEjIBGtQiCGIBCthCEkIBAhEiAMISBBgoCAgHghEAwBCyAAKAJYIhZBCHYhFSANrUIghiAMrYQhISAPrUIghiAOrYQhIyARrUIghiAQrYQhJCAWrUIghiAANQJUhCEiIAAtAFwhGCAQIRIgDCEgQYGAgIB4IRALIAEgADsAUSABQdMAaiAAQRB2OgAAIAEgFTsARSABQccAaiAVQRB2OgAAIAEgJTcCXCABIBw3AlQgASAYOgBQIAEgIjcDSCABIBY6AEQgASAgNgJAIAEgITcDOCABIA42AjQgASAjNwIsIAEgEjYCKCABICQ3AyAgASAQNgIcIAFBgICAgHg2AhgLIAEgAzoAoAEgASAENgKcASABIAU2ApgBIAEgBDYClAEgASAGNgKQASABIAc2AowBIAEgBjYCiAEgASAINgKEASABIAk2AoABIAEgCDYCfCABIAo2AnggASALNgJ0IAEgCjYCcCABIAI3A2ggASABQegAaiABQRhqEJKAgIAAAkACQAJAIAEoAgBBgICAgHhHDQAjgYCAgABBgIXAgABqIgQgAS0AEToABEEBIQAgBEEBOgAAIAEtABAhCCABKAIIIQMCQCABKAIEIAEoAgwiBEsNACADIQAMAgsgBEUNAUF/I4KAgIAAIgBBgICABGoiBiAGIABJGyEFA0ACQAJAI4GAgIAAQaiFwIAAaigCACIKI4KAgIAAIAobIgAgBGoiBiAASQ0AIAYgBUsNACAGPwBBEHQiB00NASAGIAdrQRB2IAZB//8DcUEAR2pAAEF/Rw0BC0EBIAQQtoCAgAAACyOBgICAAEGohcCAAGoiByAG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AEEADwsCQCACIAhxIgkgB2oiAiAJTw0AQQAPCwJAIAIgBk0NAEEADwsCQCACPwBBEHQiCk0NAEEAIQsgAiAKa0EQdiACQf//A3FBAEdqQABBf0YNAgsjgYCAgABBqIXAgABqIgogAiAKKAIAIgogCiAFRiIFGzYCACAFRQ0AC0EAIQsgCUUNAAJAIAMgASADIAFJGyICRQ0AIAkgACAC/AoAAAsgCSELCyALCwcAA0AMAAsLpxQEBH8Bfgh/An4jgICAgABBgAJrIgMkgICAgAACQAJAAkACQAJAAkACQAJAIAIoAgBBgICAgHhGDQBBBCEEQQAhBQwBC0EAIQVBBCEEAkACQAJAAkACQCACKAIEIgZBgICAgHhzQQQgBkEASBsOBgABBQUCAwULIANB0AFqIAJBFGoQuoCAgAAgA0HcAWogAkEgahC6gICAACADQegBaiACQSxqELqAgIAAIANBEGpBCGogA0HQAWpBFGooAgA2AgAgA0EIaiADQdABakEgaigCADYCACADIAMpAtwBNwMQIAMgAykC6AE3AwAMAwsgA0HQAWogAkEUahC6gICAACADQdwBaiACQSBqELqAgIAAIANB6AFqIAJBLGoQuoCAgAAgA0EQakEIaiADQdABakEUaigCADYCACADQQhqIANB0AFqQSBqKAIANgIAIAMgAykC3AE3AxAgAyADKQLoATcDAAwCCyADQdABaiACQRBqELqAgIAAIANB3AFqIAJBHGoQuoCAgAAgA0HoAWogAkEoahC6gICAACADQRBqQQhqIANB5AFqKAIANgIAIANBCGogA0HwAWooAgA2AgAgAyADKQLcATcDECADIAMpAugBNwMADAELIANB0AFqIAJBCGoQuoCAgAAgA0HcAWogAkEUahC6gICAACADQegBaiACQSBqELqAgIAAIANBEGpBCGogA0HQAWpBFGooAgA2AgAgA0EIaiADQdABakEgaigCADYCACADIAMpAtwBNwMQIAMgAykC6AE3AwALIAMpAtQBIQcgAygC0AEiCEGAgICAeEYNACADQSBqQQhqIANBEGpBCGooAgA2AgAgAyADKQMQNwMgIANBMGpBCGogA0EIaigCADYCACADIAMpAwA3AzBBfyOCgICAACICQYCAgARqIgUgBSACSRshBhCrgICAAANAI4GAgIAAQaiFwIAAaigCACIFI4KAgIAAIAUbIgJBfEsNByACQbR/Sw0HIAJBA2pBfHEiBEHIAGoiAiAGSw0HAkAgAj8AQRB0IglNDQAgAiAJa0EQdiACQfz/A3FBAEdqQABBf0YNCAsjgYCAgABBqIXAgABqIgkgAiAJKAIAIgkgCSAFRiIFGzYCACAFRQ0ACyAERQ0GIANB4ABqQQhqIANBIGpBCGooAgA2AgAgAyADKQMgNwNgIANB8ABqQQhqIANBMGpBCGooAgA2AgAgAyADKQMwNwNwIANB0AFqEICAgIAAAkACQAJAAkAgAy0A0AFBAXENACADKALYASICQYCAgIB4Rw0BC0EVIQYgA0HQAWpBFUEAQQFBARCpgICAACADKALUASEJIAMoAtABQQFGDQgjgYCAgAAhBSADKALYASICIAVB84HAgABqIgUpAAA3AAAgAkENaiAFQQ1qKQAANwAAIAJBCGogBUEIaikAADcAAEGBKiEKDAELQQAhCkEAIQtBACEMAkAgAkETSQ0AQQAhCkEAIQtBACEMIAMoAtQBIgItAApB1ABHDQBBACEKQQAhC0EAIQwgAi0ADUE6Rw0AQQAhCkEAIQtBACEMIAItABBBOkcNACACLQALIgVBCmwgAi0ADCIJakFwakH/AXFBkBxsQQAgCUFQakH/AXFBCkkbQQAgBUFQakH/AXFBCkkbIAMpAuABQiCIp2ogAi0ADiIFQQpsIAItAA8iCWpBcGpB/wFxQTxsQQAgCUFQakH/AXFBCkkbQQAgBUFQakH/AXFBCkkbaiACLQARIgVBCmwgAi0AEiICakFwakH/AXFBACACQVBqQf8BcUEKSRtBACAFQVBqQf8BcUEKSRtqQYCjBW8iAkGAowVqIAIgAkEASBsiAiACQTxuIgVBPGxrIQogAkGQHG4hDCAFQTxwIQsLIANB0AFqQQpBAEEBQQEQqYCAgAAgAygC1AEhBSADKALQAUEBRg0GI4GAgIAAIQkgAygC2AEiAiAJQemBwIAAaiIJKQAANwAAIAJBCGogCUEIai8AADsAACADQQo2AogBIAMgAjYChAEgAyAFNgKAASADQdABakEQQQBBAUEBEKmAgIAAIAMoAtQBIQ0gAygC0AFBAUYNBSOBgICAACECIAMoAtgBIgkgAkHMgcCAAGoiAikAADcAACAJQQhqIAJBCGopAAA3AAAgA0GMAWogA0GAAWoQuoCAgAAQq4CAgAADQCOBgICAAEGohcCAAGooAgAiBSOCgICAACAFGyICQXhLDQUgAkGYf0sNBSACQQdqQXhxIg5B4ABqIgIgBksNBQJAIAI/AEEQdCIPTQ0AIAIgD2tBEHYgAkH4/wNxQQBHakAAQX9GDQYLI4GAgIAAQaiFwIAAaiIPIAIgDygCACIPIA8gBUYiBRs2AgAgBUUNAAsgDkUNBCADI4GAgIAAIgZBtILAgABqIgIgDEH/AXFBCm4iBUEJIAVBCUkbQQJ0aigCADYCuAEgAyACIAwgBUEKbGtB/wFxQQJ0aigCADYCvAEgAyACIAtBCm4iBUECdGooAgA2AsABIAMgAiALIAVBCmxrQf8BcUECdGooAgA2AsQBIAMgAiAKQf8BcUEKbiIFQQJ0aigCADYCyAEgAyACIAogBUEKbGtB/wFxQQJ0aigCADYCzAEgAyODgICAAK1CIIYiECADQcwBaq2ENwP4ASADIBAgA0HIAWqthDcD8AEgAyAQIANBxAFqrYQ3A+gBIAMgECADQcABaq2ENwPgASADIBAgA0G8AWqthDcD2AEgAyAQIANBuAFqrYQ3A9ABIANBqAFqIAZBq4DAgABqIANB0AFqELiAgIAAIANBgAFqQQhqKAIAIQIgAykCgAEhECAOIAMpA6gBNwMAIA5BCGogA0GoAWpBCGooAgA2AgAgDkGAgICAeDYCSCAOQYCAgIB4NgIQIA5BAzoADCAOIBA3AlQgDkHcAGogAjYCACADKAKQASEKIAMoAowBIQZBECECIA1BgICAgHhHDQELIAAgCjYCECAAIAY2AgwgACACNgIIIAAgCTYCBCAAQYCAgIB4NgIADAILIAMoApQBIQIgA0HQAGpBCGogA0HgAGpBCGooAgAiBTYCACADQcAAakEIaiADQfAAakEIaigCACILNgIAIAMgAykDYCIQNwNQIAMgAykDcCIRNwNAIAQgBzcCBCAEIAg2AgAgBCAQNwIMIARBFGogBTYCACAEIBE3AhggBEEgaiALNgIAQQEhBSAEQQE2AkQgBCAONgJAIARBATYCPCAEIAI2AjggBCAKNgI0IAQgBjYCMCAEQRA2AiwgBCAJNgIoIAQgDTYCJAsgAEGAgICAeDYCDCAAIAU2AgggACAENgIEIAAgBTYCAAsgA0GAAmokgICAgAAPC0EIQeAAEK+AgIAAAAsgDSADKALYARC2gICAAAALIAUgAygC2AEQtoCAgAAACyAJIAMoAtgBELaAgIAAAAtBBEHIABCvgICAAAALogUBB39BfyOCgICAACIDQYCAgARqIgQgBCADSRshBRCrgICAAAJAA0AjgYCAgABBqIXAgABqKAIAIgQjgoCAgAAgBBsiA0F8Sw0BIANBYEsNASADQQNqQXxxIgZBHGoiAyAFSw0BAkAgAz8AQRB0IgdNDQAgAyAHa0EQdiADQfz/A3FBAEdqQABBf0YNAgsjgYCAgABBqIXAgABqIgcgAyAHKAIAIgcgByAERiIEGzYCACAERQ0ACyAGRQ0AEKuAgIAAA0ACQAJAI4GAgIAAQaiFwIAAaigCACIDI4KAgIAAIAMbIgdBcksNACAHQQ1qIgQgBUsNACAEPwBBEHQiCE0NASAEIAhrQRB2IARB//8DcUEAR2pAAEF/Rw0BC0EBQQ0QtoCAgAAACyOBgICAAEGohcCAAGoiCCAEIAgoAgAiCCAIIANGIgMbNgIAIANFDQALIAcjgYCAgABBjIHAgABqIgMpAAA3AAAgB0EFaiADQQVqKQAANwAAEKuAgIAAA0ACQAJAI4GAgIAAQaiFwIAAaigCACIDI4KAgIAAIAMbIghBbUsNACAIQRJqIgQgBUsNACAEPwBBEHQiCU0NASAEIAlrQRB2IARB//8DcUEAR2pAAEF/Rw0BC0EBQRIQtoCAgAAACyOBgICAAEGohcCAAGoiCSAEIAkoAgAiCSAJIANGIgMbNgIAIANFDQALIAgjgYCAgABBmYHAgABqIgMpAAA3AAAgCEEQaiADQRBqLwAAOwAAIAhBCGogA0EIaikAADcAACAAQQA6ABAgAEEBNgIMIAAgBjYCCCAAQoCAgIAQNwIAIAZBADoAGCAGQRI2AhQgBiAINgIQIAZCjYCAgKACNwIIIAYgBzYCBCAGQQ02AgAPC0EEQRwQr4CAgAAAC9ELAQt/QX8jgoCAgAAiAUGAgIAEaiICIAIgAUkbIQMQq4CAgAADQAJAAkAjgYCAgABBqIXAgABqKAIAIgEjgoCAgAAgARsiBEFjSw0AIARBHGoiAiADSw0AIAI/AEEQdCIFTQ0BIAIgBWtBEHYgAkH//wNxQQBHakAAQX9HDQELQQFBHBC2gICAAAALI4GAgIAAQaiFwIAAaiIFIAIgBSgCACIFIAUgAUYiARs2AgAgAUUNAAsgBCOBgICAAEGrgcCAAGoiASkAADcAACAEQRhqIAFBGGooAAA2AAAgBEEQaiABQRBqKQAANwAAIARBCGogAUEIaikAADcAABCrgICAAANAAkACQCOBgICAAEGohcCAAGooAgAiASOCgICAACABGyIFQXpLDQAgBUEFaiICIANLDQAgAj8AQRB0IgZNDQEgAiAGa0EQdiACQf//A3FBAEdqQABBf0cNAQtBAUEFELaAgIAAAAsjgYCAgABBqIXAgABqIgYgAiAGKAIAIgYgBiABRiIBGzYCACABRQ0ACyAFI4GAgIAAQceBwIAAaiIBKAAANgAAIAVBBGogAUEEai0AADoAABCrgICAAANAAkACQCOBgICAAEGohcCAAGooAgAiASOCgICAACABGyIGQW9LDQAgBkEQaiICIANLDQAgAj8AQRB0IgdNDQEgAiAHa0EQdiACQf//A3FBAEdqQABBf0cNAQtBAUEQELaAgIAAAAsjgYCAgABBqIXAgABqIgcgAiAHKAIAIgcgByABRiIBGzYCACABRQ0ACyAGI4GAgIAAQcyBwIAAaiIBKQAANwAAIAZBCGogAUEIaikAADcAABCrgICAAAJAA0AjgYCAgABBqIXAgABqKAIAIgIjgoCAgAAgAhsiAUF8Sw0BIAFBVEsNASABQQNqQXxxIghBKGoiASADSw0BAkAgAT8AQRB0IgdNDQAgASAHa0EQdiABQfz/A3FBAEdqQABBf0YNAgsjgYCAgABBqIXAgABqIgcgASAHKAIAIgcgByACRiICGzYCACACRQ0ACyAIRQ0AEKuAgIAAA0ACQAJAI4GAgIAAQaiFwIAAaigCACIBI4KAgIAAIAEbIgdBcksNACAHQQ1qIgIgA0sNACACPwBBEHQiCU0NASACIAlrQRB2IAJB//8DcUEAR2pAAEF/Rw0BC0EBQQ0QtoCAgAAACyOBgICAAEGohcCAAGoiCSACIAkoAgAiCSAJIAFGIgEbNgIAIAFFDQALIAcjgYCAgABB3IHAgABqIgEpAAA3AAAgB0EFaiABQQVqKQAANwAAEKuAgIAAA0ACQAJAI4GAgIAAQaiFwIAAaigCACIBI4KAgIAAIAEbIglBb0sNACAJQRBqIgIgA0sNACACPwBBEHQiCk0NASACIAprQRB2IAJB//8DcUEAR2pAAEF/Rw0BC0EBQRAQtoCAgAAACyOBgICAAEGohcCAAGoiCiACIAooAgAiCiAKIAFGIgEbNgIAIAFFDQALIAkjgYCAgABBzIHAgABqIgEpAAA3AAAgCUEIaiABQQhqKQAANwAAEKuAgIAAA0ACQAJAI4GAgIAAQaiFwIAAaigCACIBI4KAgIAAIAEbIgpBe0sNACAKQQRqIgIgA0sNACACPwBBEHQiC00NASACIAtrQRB2IAJB//8DcUEAR2pAAEF/Rw0BC0EBQQQQtoCAgAAACyOBgICAAEGohcCAAGoiCyACIAsoAgAiCyALIAFGIgEbNgIAIAFFDQALIApB6N61qwY2AAAgAEEAOgAwIABBATYCLCAAIAg2AiggAEKQgICAEDcCICAAIAY2AhwgAEKFgICAgAI3AhQgACAFNgIQIABCnICAgNAANwIIIAAgBDYCBCAAQRw2AgAgCEEAOgAkIAhBBDYCICAIIAo2AhwgCEKQgICAwAA3AhQgCCAJNgIQIAhCjYCAgIACNwIIIAggBzYCBCAIQQ02AgAPC0EEQSgQr4CAgAAACwIACxkAIAEjgYCAgABB3ILAgABqQQsQwoCAgAALAgAL+BADCn8BfgN/I4CAgIAAQTBrIgAkgICAgAAQgYCAgAAaQQAhAUEBQQBBACAAEIKAgIAAAkACQAJAAkACQAJAAkACQAJAAkAgAC0AAEEBcQ0AIAAoAggiAkEFdCEDIAJB////P0sNASADQfj///8HSw0BIAAoAgQhBAJAAkAgAw0AQQghBUEAIQYMAQtBfyOCgICAACIBQYCAgARqIgYgBiABSRshBxCrgICAAANAQQghASOBgICAAEGohcCAAGooAgAiCCOCgICAACAIGyIGQXhLDQMgBkEHakF4cSIFIANqIgYgBUkNAyAGIAdLDQMCQCAGPwBBEHQiCU0NACAGIAlrQRB2IAZB+P8DcUEAR2pAAEF/Rg0ECyOBgICAAEGohcCAAGoiCSAGIAkoAgAiCSAJIAhGIggbNgIAIAhFDQALIAIhBiAFRQ0CCyAAQQA2AiggACAFNgIkIAAgBjYCICACRQ0AQQAhBkEAIQEDQCAEQRBqKQMAIQogBEEMaigCACEFIARBCGooAgAhByAEQQRqKAIAIQggBCgCACEJAkAgASAAKAIgRw0AIABBIGoQpICAgAALIAAoAiQgBmoiAyAKNwMAIANBHGogBTYCACADQRhqIAc2AgAgA0EUaiAFNgIAIANBEGogCDYCACADQQxqIAk2AgAgA0EIaiAINgIAIAAgAUEBaiIBNgIoIARBGGohBCAGQSBqIQYgAiABRw0ACwtBAEEEEMOAgIAARQ0HIABBBEEAEK2AgIAAIAAoAgAhBCAAQShqIABBDGooAgA2AgAgACAAKQIENwMgQQBBAUEAIARBAEEBQQAgABCDgICAAAJAIAAoAiBFDQAgAEEgahCugICAAAsgAEEgahCEgICAACAAKAIoIgtBHGwhA0EAIQEgC0GkkskkSw0DIAAoAiQhBCADDQFBBCEFQQAhBgwCCyABIAMQtoCAgAAAC0F/I4KAgIAAIgFBgICABGoiBiAGIAFJGyEHEKuAgIAAA0BBBCEBI4GAgIAAQaiFwIAAaigCACIII4KAgIAAIAgbIgZBfEsNAiAGQQNqQXxxIgUgA2oiBiAFSQ0CIAYgB0sNAgJAIAY/AEEQdCIJTQ0AIAYgCWtBEHYgBkH8/wNxQQBHakAAQX9GDQMLI4GAgIAAQaiFwIAAaiIJIAYgCSgCACIJIAkgCEYiCBs2AgAgCEUNAAsgCyEGIAVFDQELIABBADYCCCAAIAU2AgQgACAGNgIAAkAgC0UNAEEAIQZBACEBA0AgBEEJai0AAEEARyEHIARBEGooAgAhBSAEQQxqKAIAIQkgBEEIai0AACECIARBBGooAgAhCCAEKAIAIQwCQCABIAAoAgBHDQAgABCngICAAAsgACgCBCAGaiIDIAg2AgAgA0EZaiACOgAAIANBGGogBzoAACADQRRqIAU2AgAgA0EQaiAJNgIAIANBDGogBTYCACADQQhqIAg2AgAgA0EEaiAMNgIAIAAgAUEBaiIBNgIIIARBFGohBCAGQRxqIQYgCyABRw0ACwsgABCFgICAACAALQAAQQFxDQMgACgCHCINQQV0IQRBACEBIA1B////P0sNBSAEQfj///8HSw0FIAAoAhghAyAEDQFBCCEFQQAhBgwCCyABIAMQtoCAgAAAC0F/I4KAgIAAIgFBgICABGoiBiAGIAFJGyEHEKuAgIAAA0BBCCEBI4GAgIAAQaiFwIAAaigCACIII4KAgIAAIAgbIgZBeEsNBCAGQQdqQXhxIgUgBGoiBiAFSQ0EIAYgB0sNBAJAIAY/AEEQdCIJTQ0AIAYgCWtBEHYgBkH4/wNxQQBHakAAQX9GDQULI4GAgIAAQaiFwIAAaiIJIAYgCSgCACIJIAkgCEYiCBs2AgAgCEUNAAsgDSEGIAVFDQMLIABBADYCKCAAIAU2AiQgACAGNgIgIA1FDQBBACEFQQAhAQNAQYCAgIB4IQcCQAJAAkACQAJAAkACQAJAAkACQCADQQhqLQAADgkJAQIDBAUGBwgACyAKQoCAgIBwgyADQRRqKAIAIgethCEKIANBEGooAgAiBkEYdiELIAZBEHYhAiAGQQh2IQwMCAsgA0EUaigCACIGrUIghiADQRBqNQIAhCEKIAZBGHYhCyAGQRB2IQIgBkEIdiEMQYGAgIB4IQcMBwsgA0EUaigCACIGrUIghiADQRBqNQIAhCEKIAZBGHYhCyAGQRB2IQIgBkEIdiEMQYKAgIB4IQcMBgsgA0EQaikDACEKQYOAgIB4IQcMBQsgA0EUaigCACIGrUIghiADQRBqNQIAhCEKIAZBGHYhCyAGQRB2IQIgBkEIdiEMQYSAgIB4IQcMBAsgA0EQai0AAEEARyEGQYWAgIB4IQcMAwsgA0EQai8BACIGQQh2IQwgA0ETai0AACELIANBEmotAAAhAkGGgICAeCEHDAILIANBEmotAAAhAiADQRFqLQAAIQwgA0EQai0AACEGQYeAgIB4IQcMAQsgA0EUaigCACIGrUIghiADQRBqNQIAhCEKIAZBGHYhCyAGQRB2IQIgBkEIdiEMQYiAgIB4IQcLIANBBGooAgAhCCADKAIAIQkCQCABIAAoAiBHDQAgAEEgahCkgICAAAsgACgCJCAFaiIEIAc2AgAgBEEYaiAINgIAIARBFGogCTYCACAEQRBqIAg2AgAgBEEIaiAKNwMAIARBB2ogCzoAACAEQQZqIAI6AAAgBEEFaiAMOgAAIARBBGogBjoAACAAIAFBAWoiATYCKCADQRhqIQMgBUEgaiEFIA0gAUcNAAsLIABBMGokgICAgAAPCyOBgICAACIEQYiCwIAAakErIAAgBEGYhMCAAGogBEGohMCAAGoQxICAgAAACyABIAQQtoCAgAAACwIACwoAIAAQiYCAgAALAgALqgIBB39BACEEAkAgA0UNACADIAJBASACQQFLGyICEMOAgIAARQ0AQX8jgoCAgAAiBEGAgIAEaiIFIAUgBEkbIQZBACACayEHIAJBf2ohBQNAAkAgBSOBgICAAEGohcCAAGooAgAiCCOCgICAACAIG2oiAiAFTw0AQQAPCwJAIAIgB3EiCSADaiICIAlPDQBBAA8LAkAgAiAGTQ0AQQAPCwJAIAI/AEEQdCIKTQ0AQQAhBCACIAprQRB2IAJB//8DcUEAR2pAAEF/Rg0CCyOBgICAAEGohcCAAGoiBCACIAQoAgAiBCAEIAhGIgQbNgIAIARFDQALAkAgAA0AIAkPCyAJIQQgCUUNAAJAIAMgASADIAFJGyIDRQ0AIAkgACAD/AoAAAsgCQ8LIAQLSgEDf0EAIQMCQCACRQ0AAkADQCAALQAAIgQgAS0AACIFRw0BIABBAWohACABQQFqIQEgAkF/aiICRQ0CDAALCyAEIAVrIQMLIAMLCAAQioCAgAALKwEBfyOBgICAACEAEKyAgIAAIABBgIXAgABqIgBBADoACCAAQQA6AAAgAAsKACAAEIuAgIAACyQAIAAgASACIAMgBCAFIAYgByAIIAkgCiALIAwgDRCIgICAAAsyAQF/I4GAgIAAIQ4QrICAgAAgDkGAhcCAAGoiDiANNwMQIA5BADoACCAOQQA6AAAgDgskACAAIAEgAiADIAQgBSAGIAcgCCAJIAogCyAMIA0QjICAgAALVwEBfyOAgICAAEEQayIBJICAgIAAIAFBCGogACAAKAIAQQhBIBClgICAAAJAIAEoAggiAEGBgICAeEYNACAAIAEoAgwQtoCAgAAACyABQRBqJICAgIAAC6YBAQJ/I4CAgIAAQRBrIgUkgICAgAAgBUEEaiABKAIAIgYgASgCBCACQQFqIgIgBkEBdCIGIAIgBksbIgJBBCACQQRLGyICIAMgBBCogICAAAJAAkAgBSgCBEEBRw0AIAUoAgwhASAFKAIIIQIMAQsgBSgCCCEEIAEgAjYCACABIAQ2AgRBgYCAgHghAgsgACABNgIEIAAgAjYCACAFQRBqJICAgIAAC1cBAX8jgICAgABBEGsiASSAgICAACABQQhqIAAgACgCAEEEQQwQpYCAgAACQCABKAIIIgBBgYCAgHhGDQAgACABKAIMELaAgIAAAAsgAUEQaiSAgICAAAtXAQF/I4CAgIAAQRBrIgEkgICAgAAgAUEIaiAAIAAoAgBBBEEcEKWAgIAAAkAgASgCCCIAQYGAgIB4Rg0AIAAgASgCDBC2gICAAAALIAFBEGokgICAgAALrQQDBH8BfgR/QQEhBkEEIQcCQAJAIARBf2oiCCAFakEAIARrIglxrSADrX4iCkIgiKdFDQBBACEDDAELAkAgCqciA0GAgICAeCAEa00NAEEAIQMMAQsCQAJAAkACQAJAIAFFDQBBfyOCgICAACIHQYCAgARqIgsgCyAHSRshDCADQQEgA0EBSxshCyAFIAFsIQ0DQCOBgICAAEGohcCAAGooAgAiASOCgICAACABGyIHIAhqIgUgB0kNAyAFIAlxIgUgC2oiByAFSQ0DIAcgDEsNAwJAIAc/AEEQdCIOTQ0AIAcgDmtBEHYgB0H//wNxQQBHakAAQX9GDQQLI4GAgIAAQaiFwIAAaiIOIAcgDigCACIOIA4gAUYiARs2AgAgAUUNAAsgBUUNAiANRQ0BIAUgAiAN/AoAAAwBCwJAIAMNACAEIQUMAwtBfyOCgICAACIHQYCAgARqIgUgBSAHSRshAhCrgICAAANAI4GAgIAAQaiFwIAAaigCACIBI4KAgIAAIAEbIgcgCGoiBSAHSQ0CIAUgCXEiBSADaiIHIAVJDQIgByACSw0CAkAgBz8AQRB0IgtNDQAgByALa0EQdiAHQf//A3FBAEdqQABBf0YNAwsjgYCAgABBqIXAgABqIgsgByALKAIAIgsgCyABRiIBGzYCACABRQ0ACwsgBQ0BCyAAIAQ2AgQMAQsgACAFNgIEQQAhBgtBCCEHCyAAIAdqIAM2AgAgACAGNgIAC4YEAwJ/AX4EfwJAAkAgA0F/aiIFIARqQQAgA2siBnGtIAGtfiIHQiCIpw0AIAenIgRBgICAgHggA2tNDQELIABBADYCBCAAQQE2AgAPCwJAAkACQAJAAkAgBEUNAEF/I4KAgIAAIghBgICABGoiCSAJIAhJGyEJEKuAgIAAIAJFDQEDQCOBgICAAEGohcCAAGooAgAiCiOCgICAACAKGyICIAVqIgggAkkNBCAIIAZxIgIgBGoiCCACSQ0EIAggCUsNBAJAIAg/AEEQdCILTQ0AIAggC2tBEHYgCEH//wNxQQBHakAAQX9GDQULI4GAgIAAQaiFwIAAaiILIAggCygCACILIAsgCkYiChs2AgAgCkUNAAsgAkUNAyAERQ0CIAJBACAE/AsADAILIAAgAzYCCCAAQQA2AgQgAEEANgIADwsDQCOBgICAAEGohcCAAGooAgAiCiOCgICAACAKGyICIAVqIgggAkkNAiAIIAZxIgIgBGoiCCACSQ0CIAggCUsNAgJAIAg/AEEQdCILTQ0AIAggC2tBEHYgCEH//wNxQQBHakAAQX9GDQMLI4GAgIAAQaiFwIAAaiILIAggCygCACILIAsgCkYiChs2AgAgCkUNAAsLIAINAQsgACAENgIIIAAgAzYCBCAAQQE2AgAPCyAAIAI2AgggACABNgIEIABBADYCAAsCAAsDAA8LNQEBfwJAI4GAgIAAQayFwIAAai0AAA0AI4GAgIAAIQAQhoCAgAAgAEGshcCAAGpBAToAAAsLUwEBfwJAIAJFDQAQq4CAgAACQCACIAEQjoCAgAAiA0UNACAAIAM2AgwgACACNgIIIAAgATYCBCAAIAM2AgAPCyABIAIQr4CAgAAACyAAQgA3AgALFwAgACgCCCAAKAIEIAAoAgAQj4CAgAALDQAgASAAEIeAgIAAAAsbACAAI4GAgIAAQbiEwIAAaiABIAIQu4CAgAALIAEBfwJAIAAoAgAiAUUNACAAKAIEIAFBARCPgICAAAsLGQAgASOBgICAAEHngsCAAGpBBRDCgICAAAulAgEGfyAAKAIIIQICQAJAIAFBgAFPDQBBASEDDAELAkAgAUGAEE8NAEECIQMMAQtBA0EEIAFBgIAESRshAwsgAiEEAkAgAyAAKAIAIAJrTQ0AIAAgAiADELSAgIAAIAAoAgghBAsgACgCBCAEaiEEAkACQAJAIAFBgAFJDQAgAUE/cUGAf3IhBSABQQZ2IQYgAUGAEEkNASABQQx2IQcgBkE/cUGAf3IhBgJAIAFBgIAESQ0AIAQgBToAAyAEIAY6AAIgBCAHQT9xQYB/cjoAASAEIAFBEnZBcHI6AAAMAwsgBCAFOgACIAQgBjoAASAEIAdB4AFyOgAADAILIAQgAToAAAwBCyAEIAU6AAEgBCAGQcABcjoAAAsgACADIAJqNgIIQQALnwEBAX8jgICAgABBEGsiAySAgICAAAJAIAIgAWoiASACTw0AQQBBABC2gICAAAALIANBBGogACgCACICIAAoAgQgASACQQF0IgIgASACSxsiAkEIIAJBCEsbIgIQt4CAgAACQCADKAIEQQFHDQAgAygCCCADKAIMELaAgIAAAAsgAygCCCEBIAAgAjYCACAAIAE2AgQgA0EQaiSAgICAAAtQAQF/AkAgAiAAKAIAIAAoAggiA2tNDQAgACADIAIQtICAgAAgACgCCCEDCwJAIAJFDQAgACgCBCADaiABIAL8CgAACyAAIAMgAmo2AghBAAscAAJAIABFDQAgACABEK+AgIAAAAsQuYCAgAAAC5ABAAJAAkAgA0EATg0AQQEhAUEEIQJBACEDDAELAkACQAJAAkAgAUUNACACIAFBASADEJCAgIAAIQEMAQsCQCADDQBBASEBDAILEKuAgIAAIANBARCOgICAACEBCyABDQBBASEBIABBATYCBAwBCyAAIAE2AgRBACEBC0EIIQILIAAgAmogAzYCACAAIAE2AgAL0QMBBn8jgICAgABBEGsiAySAgICAAAJAAkACQAJAAkACQCACQQFxDQAgAS0AACIERQ0CQQAhBSABIQZBACEHA0AgBkEBaiEGAkACQCAEwEF/Sg0AAkAgBEH/AXFBgAFGDQAgBiAEQQNxQRh3IghBBXRBgICAgARxIAhBgICACHFBB3QgCEGAgICAAnFyckEddmogBEEBdkECcWogBEECdkECcWohBiAHRSAFciEFDAILIAcgBi8AACIEaiEHIAYgBGpBAmohBgwBCyAGIARB/wFxIgRqIQYgByAEaiEHCyAGLQAAIgQNAAtBACEEIAUgB0EQSXENAUEAIQggB0EBdCIEQQBODQEMBQsgAkEBdiEECyAEDQELQQEhBkEAIQQMAQsQq4CAgABBASEIIARBARCOgICAACIGRQ0BCyADQQA2AgggAyAGNgIEIAMgBDYCAAJAIAMjgYCAgABBuITAgABqIAEgAhC7gICAAEUNACOBgICAACIEQeyCwIAAakHWACADQQ9qIARB0ITAgABqIARB4ITAgABqEMSAgIAAAAsgACADKQIANwIAIABBCGogA0EIaigCADYCACADQRBqJICAgIAADwsgCCAEELaAgIAAAAslAQF/I4GAgIAAIgBBwoPAgABqQSMgAEHwhMCAAGoQwICAgAAAC2sBAn8gASgCBCECAkACQAJAIAEoAggiAQ0AQQEhAwwBCxCrgICAACABQQEQjoCAgAAiA0UNAQsCQCABRQ0AIAMgAiAB/AoA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MTVleHBlcmltZW50YWxfdjA1Y2xvY2s4d2FsbF9ub3cxMXdpdF9pbXBvcnQxMTdoZTNmOTFmYmE2YzNkM2YyNUUBb19aTjI4YWlfdmliYXBwX2N1c3RvbV8xYTA0NDQyNjEwYzZ2aWJhcHAxNWV4cGVyaW1lbnRhbF92MDVjbG9jazEzbW9ub3RvbmljX25vdzExd2l0X2ltcG9ydDAxN2g1ODQzOTM5ODA4MzgzMTcyRQJqX1pOMjhhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwMmt2MTFzY2FuX3ByZWZpeDExd2l0X2ltcG9ydDIxN2g1MTliMWMyNDhkOThiMDk5RQNkX1pOMjhhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwM2xvZzV3cml0ZTExd2l0X2ltcG9ydDcxN2gwMjVjMmU1MDQ3YTc0MGUxRQRzX1pOMjhhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwOWhvc3RfaW5mbzEzZGVzY3JpYmVfaG9zdDExd2l0X2ltcG9ydDExN2g2YmE5NDhkNmM3MzVkYjFkRQVrX1pOMjhhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwOHNldHRpbmdzN2N1cnJlbnQxMXdpdF9pbXBvcnQxMTdoNDRjZTY1NWU0OWUzNTgxZkUGEV9fd2FzbV9jYWxsX2N0b3JzB0tzaWduYXR1cmVfbWlzbWF0Y2g6X1JOdkNzaFh3RmxsWDU2cFRfN19fX3J1c3RjMjZfX19ydXN0X2FsbG9jX2Vycm9yX2hhbmRsZXIIcF9aTjI4YWlfdmliYXBwX2N1c3RvbV8xYTA0NDQyNjEwYzdleHBvcnRzNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwNWd1ZXN0MTlfZXhwb3J0X2hlYWx0aF9jYWJpMTdoMDY4MzE0ZWViNjYzNjAzMUUJcV9aTjI4YWlfdmliYXBwX2N1c3RvbV8xYTA0NDQyNjEwYzdleHBvcnRzNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwNWd1ZXN0MjBfX3Bvc3RfcmV0dXJuX2hlYWx0aDE3aGM2MmJkYjMyYzc4NDFlZjZFCnJfWk4yOGFpX3ZpYmFwcF9jdXN0b21fMWEwNDQ0MjYxMGM3ZXhwb3J0czZ2aWJhcHAxNWV4cGVyaW1lbnRhbF92MDVndWVzdDIxX2V4cG9ydF9kZXNjcmliZV9jYWJpMTdoYTJmZDAxMjU3MzYzNjZkY0ULdl9aTjI4YWlfdmliYXBwX2N1c3RvbV8xYTA0NDQyNjEwYzdleHBvcnRzNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwNWd1ZXN0MjVfZXhwb3J0X2hhbmRsZV9ldmVudF9jYWJpMTdoMWQxZWJjYzFlMDllZjhhM0UMe19aTjI4YWlfdmliYXBwX2N1c3RvbV8xYTA0NDQyNjEwYzdleHBvcnRzNnZpYmFwcDE1ZXhwZXJpbWVudGFsX3YwNWd1ZXN0MzBfZXhwb3J0X3ZhbGlkYXRlX3NldHRpbmdzX2NhYmkxN2hhNGNjODhiNzhiYzc5N2ZiRQ1iX1pONjlfJExUJGNvcmUuLmFsbG9jLi5sYXlvdXQuLkxheW91dEVycm9yJHUyMCRhcyR1MjAkY29yZS4uZm10Li5EZWJ1ZyRHVCQzZm10MTdoNmUwMzI4MjI4OTMzNTZmNEUOKl9STnZDc2hYd0ZsbFg1NnBUXzdfX19ydXN0YzEyX19fcnVzdF9hbGxvYw8sX1JOdkNzaFh3RmxsWDU2cFRfN19fX3J1c3RjMTRfX19ydXN0X2RlYWxsb2MQLF9STnZDc2hYd0ZsbFg1NnBUXzdfX19ydXN0YzE0X19fcnVzdF9yZWFsbG9jES5fUk52Q3NoWHdGbGxYNTZwVF83X19fcnVzdGMxN3J1c3RfYmVnaW5fdW53aW5kErABX1pOMTM2XyRMVCRhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjLi5Db21wb25lbnQkdTIwJGFzJHUyMCRhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjLi5leHBvcnRzLi52aWJhcHAuLmV4cGVyaW1lbnRhbF92MC4uZ3Vlc3QuLkd1ZXN0JEdUJDEyaGFuZGxlX2V2ZW50MTdoNDk4Y2ExZDc0YjA0MzA2OUUTqQFfWk4xMzZfJExUJGFpX3ZpYmFwcF9jdXN0b21fMWEwNDQ0MjYxMGMuLkNvbXBvbmVudCR1MjAkYXMkdTIwJGFpX3ZpYmFwcF9jdXN0b21fMWEwNDQ0MjYxMGMuLmV4cG9ydHMuLnZpYmFwcC4uZXhwZXJpbWVudGFsX3YwLi5ndWVzdC4uR3Vlc3QkR1QkNmhlYWx0aDE3aDYzMGFlZDIxOGNlYjdiMDBFFKsBX1pOMTM2XyRMVCRhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjLi5Db21wb25lbnQkdTIwJGFzJHUyMCRhaV92aWJhcHBfY3VzdG9tXzFhMDQ0NDI2MTBjLi5leHBvcnRzLi52aWJhcHAuLmV4cGVyaW1lbnRhbF92MC4uZ3Vlc3QuLkd1ZXN0JEdUJDhkZXNjcmliZTE3aDZhOTlmNjcyZGM2ZTdjODBFFV9fWk4yOGFpX3ZpYmFwcF9jdXN0b21fMWEwNDQ0MjYxMGM0MF9fbGlua19jdXN0b21fc2VjdGlvbl9kZXNjcmliaW5nX2ltcG9ydHMxN2g1MGFhNDNhMjE0ZDM2ZjIxRRZiX1pONjlfJExUJGNvcmUuLmFsbG9jLi5sYXlvdXQuLkxheW91dEVycm9yJHUyMCRhcyR1MjAkY29yZS4uZm10Li5EZWJ1ZyRHVCQzZm10MTdoNmUwMzI4MjI4OTMzNTZmNEUXZF9aTjcwXyRMVCRhbGxvYy4udmVjLi5WZWMkTFQkVCRDJEEkR1QkJHUyMCRhcyR1MjAkY29yZS4ub3BzLi5kcm9wLi5Ecm9wJEdUJDRkcm9wMTdoMGEwYTczNjdjMDIyYTY5OEUYH19fdmliYXBwX2ZvcmNlX2RlY2xhcmVkX2ltcG9ydHMZNWNhYmlfcG9zdF92aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI2Rlc2NyaWJlGjljYWJpX3Bvc3RfdmliYXBwOmV4cGVyaW1lbnRhbC12MC9ndWVzdEAwLjAuMSNoYW5kbGUtZXZlbnQbNGNhYmlfcG9zdF92aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI21pZ3JhdGUcDGNhYmlfcmVhbGxvYx0GbWVtY21wHit2aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI2Rlc2NyaWJlHzZ2aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI2dldC1zZXR0aW5ncy1zY2hlbWEgL3ZpYmFwcDpleHBlcmltZW50YWwtdjAvZ3Vlc3RAMC4wLjEjaGFuZGxlLWV2ZW50ISl2aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI2hlYWx0aCIqdmliYXBwOmV4cGVyaW1lbnRhbC12MC9ndWVzdEAwLjAuMSNtaWdyYXRlIzR2aWJhcHA6ZXhwZXJpbWVudGFsLXYwL2d1ZXN0QDAuMC4xI3ZhbGlkYXRlLXNldHRpbmdzJENfWk41YWxsb2M3cmF3X3ZlYzE5UmF3VmVjJExUJFQkQyRBJEdUJDhncm93X29uZTE3aDMxMGNiYjI5OTZkZDRjNGZFJUtfWk41YWxsb2M3cmF3X3ZlYzIwUmF3VmVjSW5uZXIkTFQkQSRHVCQxNGdyb3dfYW1vcnRpemVkMTdoNTFhYjM4YjQ4ZDczMDEzYUUmQ19aTjVhbGxvYzdyYXdfdmVjMTlSYXdWZWMkTFQkVCRDJEEkR1QkOGdyb3dfb25lMTdoNjQyOGQ3MWM2NDE5OWI4OUUnQ19aTjVhbGxvYzdyYXdfdmVjMTlSYXdWZWMkTFQkVCRDJEEkR1QkOGdyb3dfb25lMTdoODcyNzE5MDEwZTdlZTU2MUUoSF9aTjVhbGxvYzdyYXdfdmVjMjBSYXdWZWNJbm5lciRMVCRBJEdUJDExZmluaXNoX2dyb3cxN2g4NTQ3Y2I5NjU0YTExNWVmRSlMX1pONWFsbG9jN3Jhd192ZWMyMFJhd1ZlY0lubmVyJExUJEEkR1QkMTV0cnlfYWxsb2NhdGVfaW4xN2g1MTRiNzVlY2FiNWQxMTA4RSp0X1pOODZfJExUJGFsbG9jLi52ZWMuLmludG9faXRlci4uSW50b0l0ZXIkTFQkVCRDJEEkR1QkJHUyMCRhcyR1MjAkY29yZS4ub3BzLi5kcm9wLi5Ecm9wJEdUJDRkcm9wMTdoMWU4NDA4NzdhMDMwYzdhNEUrQV9STnZDc2hYd0ZsbFg1NnBUXzdfX19ydXN0YzM1X19fcnVzdF9ub19hbGxvY19zaGltX2lzX3Vuc3RhYmxlX3YyLDdfWk4xMXdpdF9iaW5kZ2VuMnJ0MTRydW5fY3RvcnNfb25jZTE3aGQxODZiNWMxMDQyMzNhNDNFLTNfWk4xMXdpdF9iaW5kZ2VuMnJ0N0NsZWFudXAzbmV3MTdoNDI1NmJkMjE2NDQxOTA3OEUuYF9aTjY2XyRMVCR3aXRfYmluZGdlbi4ucnQuLkNsZWFudXAkdTIwJGFzJHUyMCRjb3JlLi5vcHMuLmRyb3AuLkRyb3AkR1QkNGRyb3AxN2hkMjRlMzFmZDU3MDk1YmIzRS83X1pONWFsbG9jNWFsbG9jMThoYW5kbGVfYWxsb2NfZXJyb3IxN2hhZmZlYjIzNjJhYjQ3MDZhRTAwX1pONGNvcmUzZm10NVdyaXRlOXdyaXRlX2ZtdDE3aGVjNDAxMjNlYTYwNTZlOTBFMUxfWk40Y29yZTNwdHI0MmRyb3BfaW5fcGxhY2UkTFQkYWxsb2MuLnN0cmluZy4uU3RyaW5nJEdUJDE3aDA0Yjc1ZGY5OWM2MGY1YTNFMlJfWk41M18kTFQkY29yZS4uZm10Li5FcnJvciR1MjAkYXMkdTIwJGNvcmUuLmZtdC4uRGVidWckR1QkM2ZtdDE3aDFiNjU1MzZjZDMzYTFlMWNFM19fWk41OF8kTFQkYWxsb2MuLnN0cmluZy4uU3RyaW5nJHUyMCRhcyR1MjAkY29yZS4uZm10Li5Xcml0ZSRHVCQxMHdyaXRlX2NoYXIxN2gwMDUxZTZhZjdhNzUxZWY4RTRaX1pONWFsbG9jN3Jhd192ZWMyMFJhd1ZlY0lubmVyJExUJEEkR1QkN3Jlc2VydmUyMWRvX3Jlc2VydmVfYW5kX2hhbmRsZTE3aDZlN2FmMTc0YmFkYThlZTNFNV1fWk41OF8kTFQkYWxsb2MuLnN0cmluZy4uU3RyaW5nJHUyMCRhcyR1MjAkY29yZS4uZm10Li5Xcml0ZSRHVCQ5d3JpdGVfc3RyMTdoMDg4ZDI5NzI0MTk5ZGEzYkU2M19aTjVhbGxvYzdyYXdfdmVjMTJoYW5kbGVfZXJyb3IxN2hjMzEwMzIwYWUwNTY4ZGIxRTdIX1pONWFsbG9jN3Jhd192ZWMyMFJhd1ZlY0lubmVyJExUJEEkR1QkMTFmaW5pc2hfZ3JvdzE3aGU0OTJkZDcwM2ExNDVhNWZFODZfWk41YWxsb2MzZm10NmZvcm1hdDEyZm9ybWF0X2lubmVyMTdoNDc5Y2ZiNTY5N2YxZmM3OEU5OF9aTjVhbGxvYzdyYXdfdmVjMTdjYXBhY2l0eV9vdmVyZmxvdzE3aDQ2Nzk5ZWFjZjE1Y2ZmODNFOltfWk42MF8kTFQkYWxsb2MuLnN0cmluZy4uU3RyaW5nJHUyMCRhcyR1MjAkY29yZS4uY2xvbmUuLkNsb25lJEdUJDVjbG9uZTE3aDE5ZGY2Yjg0ZGM0ZjNhMTRFOyZfWk40Y29yZTNmbXQ1d3JpdGUxN2hkZmIwMWNhMjBiM2YxNGEwRTxHX1pONDJfJExUJCRSRiRUJHUyMCRhcyR1MjAkY29yZS4uZm10Li5EZWJ1ZyRHVCQzZm10MTdoZTc2YmEzYThkMzdiZGQ3NEU9Ll9aTjRjb3JlM2ZtdDlGb3JtYXR0ZXIzcGFkMTdoMzY5YjAyYzE0NzlhYTY1ZEU+SF9aTjQzXyRMVCRjaGFyJHUyMCRhcyR1MjAkY29yZS4uZm10Li5EaXNwbGF5JEdUJDNmbXQxN2g1YWRiNzliYmUyNjE3MzgxRT9JX1pONDRfJExUJCRSRiRUJHUyMCRhcyR1MjAkY29yZS4uZm10Li5EaXNwbGF5JEdUJDNmbXQxN2gwZjYyOWFkNjExYjc3ZDA5RUAwX1pONGNvcmU5cGFuaWNraW5nOXBhbmljX2ZtdDE3aGZlOGJmN2Y5M2U5MjVmMWJFQTZfWk40Y29yZTNzdHI1Y291bnQxNGRvX2NvdW50X2NoYXJzMTdoMzkxZTE5ZjIyYzBhODNjNUVCNF9aTjRjb3JlM2ZtdDlGb3JtYXR0ZXI5d3JpdGVfc3RyMTdoYTQyMzJhYWRkYjU2MTQxM0VDRV9aTjRjb3JlNWFsbG9jNmxheW91dDZMYXlvdXQxOWlzX3NpemVfYWxpZ25fdmFsaWQxN2hiYjNkNjJlYzA3NDYwNzNiRUQyX1pONGNvcmU2cmVzdWx0MTN1bndyYXBfZmFpbGVkMTdoY2MyNWM0MjBkMmJjMjhhMkUHzgEFAA9fX3N0YWNrX3BvaW50ZXIBH0dPVC5kYXRhLmludGVybmFsLl9fbWVtb3J5X2Jhc2UCHUdPVC5kYXRhLmludGVybmFsLl9faGVhcF9iYXNlA1pHT1QuZnVuYy5pbnRlcm5hbC5fWk40M18kTFQkY2hhciR1MjAkYXMkdTIwJGNvcmUuLmZtdC4uRGlzcGxheSRHVCQzZm10MTdoNWFkYjc5YmJlMjYxNzM4MUUEHkdPVC5kYXRhLmludGVybmFsLl9fdGFibGVfYmFzZQkRAgAHLnJvZGF0YQEFLmRhdGEAewlwcm9kdWNlcnMCCGxhbmd1YWdlAQRSdXN0AAxwcm9jZXNzZWQtYnkDBXJ1c3RjHTEuOTMuMCAoMjU0YjU5NjA3IDIwMjYtMDEtMTkpDXdpdC1jb21wb25lbnQHMC4yNTQuMBB3aXQtYmluZGdlbi1ydXN0BjAuNjAuMACUAQ90YXJnZXRfZmVhdHVyZXMIKwtidWxrLW1lbW9yeSsPYnVsay1tZW1vcnktb3B0KxZjYWxsLWluZGlyZWN0LW92ZXJsb25nKwptdWx0aXZhbHVlKw9tdXRhYmxlLWdsb2JhbHMrE25vbnRyYXBwaW5nLWZwdG9pbnQrD3JlZmVyZW5jZS10eXBlcysIc2lnbi1leHQ=","app.core2.wasm":"AGFzbQEAAAABFwNgAX8AYAR/f39/AGAIf39/f39/f38AAwYFAAECAAAEBQFwAQUFByAGATAAAAExAAEBMgACATMAAwE0AAQIJGltcG9ydHMBAApHBQkAIABBABEAAAsPACAAIAEgAiADQQERAQALFwAgACABIAIgAyAEIAUgBiAHQQIRAgALCQAgAEEDEQAACwkAIABBBBEAAAsALwlwcm9kdWNlcnMBDHByb2Nlc3NlZC1ieQENd2l0LWNvbXBvbmVudAcwLjI0MS4y","app.core3.wasm":"AGFzbQEAAAABFwNgAX8AYAR/f39/AGAIf39/f39/f38AAikGAAEwAAAAATEAAQABMgACAAEzAAAAATQAAAAIJGltcG9ydHMBcAEFBQkLAQBBAAsFAAECAwQALwlwcm9kdWNlcnMBDHByb2Nlc3NlZC1ieQENd2l0LWNvbXBvbmVudAcwLjI0MS4y"};
const coreModules = new Map(Object.entries(coreBytes).map(([name, value]) => [name,
  new WebAssembly.Module(Uint8Array.from(atob(value), char => char.charCodeAt(0))) ]));
const hostImports = Object.fromEntries(["vibapp:experimental-v0/clock","vibapp:experimental-v0/host-info","vibapp:experimental-v0/kv","vibapp:experimental-v0/log","vibapp:experimental-v0/settings"].map(name => [name, host]));
function call(name, args) {
  const instance = instantiate(name => { if (!coreModules.has(name)) throw new Error('unknown-core-module'); return coreModules.get(name); }, hostImports);
  if (!instance.guest || typeof instance.guest[name] !== 'function') throw new Error('missing-guest-export');
  return instance.guest[name](...args);
}
export const guest = Object.freeze(Object.fromEntries(['describe','getSettingsSchema','validateSettings','handleEvent','health','migrate'].map(name => [name, (...args) => call(name, args)])));
