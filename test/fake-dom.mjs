// Just enough DOM for the lodge's page scripts: class selectors, one attribute form.
// A selector matches one class only: write a.querySelector('.x').querySelector('.y').
export class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.style = {};
    this.className = '';
    this._text = '';
    this._html = '';
    this.classList = { contains: cls => this.className.split(/\s+/).includes(cls) };
  }
  set textContent(value) { this._text = String(value); this._html = ''; this.children = []; }
  get textContent() { return this._text || this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { this._html = String(value); this._text = ''; this.children = []; }
  get innerHTML() { return this._html; }
  append(...nodes) { nodes.forEach(node => { node.parentNode = this; this.children.push(node); }); }
  appendChild(node) { this.append(node); return node; }
  insertBefore(node, ref) {
    node.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(node); else this.children.splice(i, 0, node);
  }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener() {}
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  getBoundingClientRect() { return { left: 0, right: 0, top: 0, bottom: 0 }; }
  focus() {}
  matches(selector) {
    const [cls] = selector.replace(/\[.*\]$/, '').split('.').filter(Boolean);
    return this.className.split(/\s+/).includes(cls);
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = node => node.children.forEach(child => {
      if (child.matches(selector)) out.push(child);
      walk(child);
    });
    walk(this);
    return out;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
