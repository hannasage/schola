import assert from 'node:assert/strict';
import test from 'node:test';
import {createShellBridge} from '../web/shell/bridge.js';
import {createAccountBridge} from '../web/services/accounts.js';

test('failed navigation preserves the selected view and exposes errors',async()=>{
 const bridge=createShellBridge(),errors=[];
 bridge.bind({navigate:async()=>false,exportProgress(){},restoreBackup(){},reportError:error=>errors.push(error.message)});
 assert.equal(await bridge.navigate('experiments'),false);assert.equal(bridge.getSnapshot().view,'hub');
 bridge.bind({navigate:async()=>{throw Error('Save failed');},exportProgress(){},restoreBackup(){},reportError:error=>errors.push(error.message)});
 assert.equal(await bridge.navigate('guide'),false);assert.deepEqual(errors,['Save failed']);
});
test('navigation cannot overlap and progress updates do not reset its selection',async()=>{
 const bridge=createShellBridge();let release;
 bridge.bind({navigate:()=>new Promise(resolve=>{release=resolve;}),exportProgress(){},restoreBackup(){},reportError(){}});
 const first=bridge.navigate('experiments');assert.equal(await bridge.navigate('guide'),false);
 release(true);assert.equal(await first,true);
 bridge.update({progress:{done:1,total:3},ready:true});assert.equal(bridge.getSnapshot().view,'experiments');
 let updates=0;const stop=bridge.subscribe(()=>updates++);bridge.update({ready:false});stop();bridge.update({ready:true});assert.equal(updates,1);
});
test('account bridge fails before mounting and preserves canceled imports',async()=>{
 const bridge=createAccountBridge();await assert.rejects(bridge.service.snapshot(),/not ready/);
 const calls=[];bridge.bind({snapshot:async()=>({state:{}}),importCloud:async()=>false,setAccountScope:async scope=>calls.push(scope),invalidateAccountScope:async()=>calls.push('invalidated'),download(){}});
 assert.equal(await bridge.service.importCloud({},{}),false);
 await bridge.service.setAccountScope(null);await bridge.service.invalidateAccountScope();assert.deepEqual(calls,[null,'invalidated']);
});
