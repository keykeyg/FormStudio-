import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {Viewer} from '../static/viewer.js';

let uploaded;
const model={gl:{ARRAY_BUFFER:1,STATIC_DRAW:2,bindBuffer(){},bufferData(t,v){uploaded=v;}},buffer:{},fit(){this.fitted=true;}};
const raw=readFileSync(new URL('../examples/sample-bracket/model.stl',import.meta.url));
Viewer.prototype.loadBuffer.call(model,raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength));
assert.equal(model.count,raw.readUInt32LE(80)*3);
assert.ok(model.fitted);
assert.ok(Math.abs(model.center[0])<1e-4&&Math.abs(model.center[1])<1e-4);
assert.ok(uploaded.every(Number.isFinite));
const text=new TextEncoder().encode('solid test\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 2 0 0\nvertex 0 2 0\nendloop\nendfacet\nendsolid');
Viewer.prototype.loadBuffer.call(model,text.buffer);
assert.equal(model.count,3);
assert.deepEqual(model.center,[1,1,0]);
assert.throws(()=>Viewer.prototype.loadBuffer.call(model,new ArrayBuffer(0)),/No triangles/);
console.log('STL viewer checks passed: binary/ASCII parsing, finite normals, camera bounds, empty-file rejection.');
