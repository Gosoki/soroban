<template>
  <!-- 侧栏迷你计算器：+ − × ÷ 与括号。手机/侧栏收起时由父级 v-if 控制不渲染。 -->
  <div class="calc">
    <input ref="inp" class="calc-in" v-model="expr" spellcheck="false" placeholder="计算器"
           @keyup.enter="equals" />
    <div class="calc-row">
      <button class="calc-tog" :class="{ open }" @click="toggle" :title="open ? '收起按键' : '展开按键'">
        <el-icon><ArrowRight /></el-icon>
      </button>
      <span class="calc-res" :class="{ err: expr && result === '' }">{{ result !== '' ? '= ' + result : (expr ? '…' : ' ') }}</span>
    </div>
    <div v-if="open" class="calc-grid">
      <button class="k fn" @click="clear">C</button>
      <button class="k fn" @click="back">⌫</button>
      <button class="k op" @click="ins('(')">(</button>
      <button class="k op" @click="ins(')')">)</button>
      <button class="k" @click="ins('7')">7</button>
      <button class="k" @click="ins('8')">8</button>
      <button class="k" @click="ins('9')">9</button>
      <button class="k op" @click="ins('÷')">÷</button>
      <button class="k" @click="ins('4')">4</button>
      <button class="k" @click="ins('5')">5</button>
      <button class="k" @click="ins('6')">6</button>
      <button class="k op" @click="ins('×')">×</button>
      <button class="k" @click="ins('1')">1</button>
      <button class="k" @click="ins('2')">2</button>
      <button class="k" @click="ins('3')">3</button>
      <button class="k op" @click="ins('-')">−</button>
      <button class="k" @click="ins('0')">0</button>
      <button class="k" @click="ins('.')">.</button>
      <button class="k eq" @click="equals">=</button>
      <button class="k op" @click="ins('+')">+</button>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'

const expr = ref('')
const inp = ref(null)
// 默认**收起**：算一笔账才偶尔用一次，常驻展开白占侧栏高度。
// 仍然记忆——手动展开过就一直展开，直到你再收起来（判据用 === '1' 而非 !== '0'，
// 这样「从没点过」和「点过又收起」都是收起，只有明确展开过才展开）。
const open = ref(localStorage.getItem('calc_open') === '1')
function toggle() { open.value = !open.value; localStorage.setItem('calc_open', open.value ? '1' : '0') }
function ins(ch) { expr.value += ch }
function back() { expr.value = expr.value.slice(0, -1) }
function clear() { expr.value = '' }
function equals() { if (result.value !== '') expr.value = result.value }

const result = computed(() => calc(expr.value))

// —— 全局捕获：没聚焦在真输入框、也没弹窗时，页面上敲数字/运算符 → 进计算器并聚焦 ——
function focusInput() {
  nextTick(() => { const el = inp.value; if (el) { el.focus(); const n = el.value.length; el.setSelectionRange(n, n) } })
}
function isEditable(el) {
  if (!el) return false
  const tag = el.tagName
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el.isContentEditable
}
// 焦点是否落在「按 Enter/空格会触发默认行为」的控件上（按钮、链接、可 Tab 到的自定义控件）。
// 这类元素上的 Enter 必须放行：preventDefault 会连带取消浏览器为它合成的 click。
function isActivatable(el) {
  if (!el || el === document.body) return false
  const tag = el.tagName
  if (tag === 'BUTTON' || tag === 'A' || tag === 'SUMMARY' || tag === 'LABEL') return true
  const role = el.getAttribute?.('role')
  if (role && ['button', 'link', 'menuitem', 'option', 'tab', 'switch', 'checkbox'].includes(role)) return true
  return el.hasAttribute?.('tabindex')          // 自定义可聚焦控件（Element Plus 大量使用）
}
function onGlobalKey(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return          // 别劫持快捷键
  if (isEditable(document.activeElement)) return           // 正在真输入框里打字 → 不抢
  if (document.body.classList.contains('el-popup-parent--hidden')) return   // 有弹窗/抽屉打开 → 不抢焦点
  const k = e.key
  // Enter / = / Backspace 在按钮、链接等可激活元素上有各自的默认行为，一律放行。
  // 否则：算式一旦非空（equals() 会把结果留在框里，且本组件挂在 Layout 内、切页不卸载），
  // 整个应用里的 Enter 就被永久吞掉——键盘用户点不动任何按钮，还会被把焦点抢到侧栏。
  if ((k === 'Enter' || k === '=' || k === 'Backspace') && isActivatable(document.activeElement)) return
  if (/^[0-9]$/.test(k) || k === '.' || k === '+' || k === '-' || k === '(' || k === ')') ins(k)
  else if (k === '*') ins('×')
  else if (k === '/') ins('÷')
  else if ((k === 'Enter' || k === '=') && expr.value) equals()
  else if (k === 'Backspace' && expr.value) back()
  else return                       // 非计算器键，或空算式下的 Enter/Backspace → 放行，别吞按钮/链接的默认行为
  e.preventDefault()
  focusInput()
}
onMounted(() => window.addEventListener('keydown', onGlobalKey))
onUnmounted(() => window.removeEventListener('keydown', onGlobalKey))

// 安全求值（不使用 eval）：调度场算法 → RPN，支持 + − × ÷ 括号、小数、一元负号。
// 非法/未写完返回 '' —— 结果行显示「…」，不报错。
function calc(input) {
  let s = (input || '').replace(/×/g, '*').replace(/÷/g, '/').replace(/\s+/g, '')
  if (!s || !/^[0-9.+\-*/()]+$/.test(s)) return ''
  // 括号隐式乘法：2(3)→2*(3)、(1+2)(3)→(1+2)*(3)、(3)2→(3)*2（不动 -(3) 这类）
  s = s.replace(/([\d.)])\(/g, '$1*(').replace(/\)([\d.])/g, ')*$1')
  const tokens = s.match(/(\d*\.\d+|\d+\.?\d*|[+\-*/()])/g) || []
  const out = [], ops = []
  const prec = { u: 3, '*': 2, '/': 2, '+': 1, '-': 1 }
  let prev = null   // 'num' | 'op' | '('
  for (let t of tokens) {
    if (/[\d.]/.test(t)) { out.push(parseFloat(t)); prev = 'num' }
    else if (t === '(') { ops.push(t); prev = '(' }
    else if (t === ')') {
      while (ops.length && ops[ops.length - 1] !== '(') out.push(ops.pop())
      if (ops.pop() !== '(') return ''      // 括号不匹配
      prev = 'num'
    } else {
      if (t === '-' && (prev === null || prev === 'op' || prev === '(')) t = 'u'   // 一元负号
      const rightAssoc = t === 'u'
      while (ops.length && ops[ops.length - 1] !== '('
        && (prec[ops[ops.length - 1]] > prec[t] || (prec[ops[ops.length - 1]] === prec[t] && !rightAssoc)))
        out.push(ops.pop())
      ops.push(t); prev = 'op'
    }
  }
  while (ops.length) { const o = ops.pop(); if (o === '(') return ''; out.push(o) }
  const st = []
  for (const t of out) {
    if (typeof t === 'number') st.push(t)
    else if (t === 'u') { const a = st.pop(); if (a === undefined) return ''; st.push(-a) }
    else {
      const b = st.pop(), a = st.pop()
      if (a === undefined || b === undefined) return ''
      st.push(t === '+' ? a + b : t === '-' ? a - b : t === '*' ? a * b : a / b)
    }
  }
  if (st.length !== 1 || !isFinite(st[0])) return ''
  return String(Math.round((st[0] + Number.EPSILON) * 1e10) / 1e10)   // 去浮点尾巴
}
</script>

<style scoped>
/* 自然块，上下内边距对称 */
.calc { padding: 10px 12px; border-top: 1px solid #1c2740; }
/* 结果行：折叠箭头在左、答案在右，同一行齐平 */
.calc-row { display: flex; align-items: center; gap: 6px; min-height: 26px; margin: 8px 0 0; }
.calc-tog { display: inline-flex; align-items: center; justify-content: center; cursor: pointer;
  width: 24px; height: 22px; padding: 0; border: none; background: transparent; border-radius: 5px;
  color: #7d8aa3; font-size: 16px; transition: transform .18s, color .15s, background .15s; }
.calc-tog:hover { background: #172236; color: #c7d2e6; }
.calc-tog.open { transform: rotate(90deg); }
.calc-in {
  width: 100%; height: 32px; box-sizing: border-box; padding: 0 10px;
  background: #0b1220; border: 1px solid #253149; border-radius: 6px;
  color: #e6edf7; font-size: 14px; outline: none;
}
.calc-in:focus { border-color: #1890ff; }
.calc-in::placeholder { color: #5b6880; font-size: 13px; }
/* 答案：靠右，和左侧折叠箭头同一行齐平 */
.calc-res {
  margin-left: auto; min-width: 0; text-align: right;
  color: #67c23a; font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.calc-res.err { color: #7d8aa3; }
.calc-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 8px; }
.k {
  height: 30px; border: 1px solid #253149; border-radius: 6px; cursor: pointer;
  background: #131c2f; color: #d6deea; font-size: 13px; padding: 0;
  display: flex; align-items: center; justify-content: center; user-select: none;
}
.k:hover { background: #1b2942; }
.k:active { background: #22314c; }
.k.op { color: #7f9cff; }
.k.fn { color: #9ba8bf; }
.k.eq { background: #1890ff; border-color: #1890ff; color: #fff; }
.k.eq:hover { background: #2b9bff; }
</style>
