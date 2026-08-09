/**
 * 上传前的图片预检：分辨率超过后端 OCR 上限的，在**本机**就拦下来。
 *
 * 后端 `services/ocr.py::MAX_OCR_PIXELS` 是硬拒绝（400），而且必须是硬的：
 * 降采样会掉小字体识别率，而**识别错的快递单号比识别不出来贵得多**——错号会去精确
 * 匹配并挂靠到别人的订单上。所以上限不打算放宽。
 *
 * 但「硬拒绝」不等于「让用户白等一趟」。触发这条闸的典型是手机长截图拼接、A3 扫描件，
 * 动辄几十 MB：现在的流程是整张传上去、后端解析出尺寸、回 400，用户在慢网络上
 * 盯着进度条等半分钟才被告知不行。而尺寸这件事**浏览器本地一读就知道**。
 *
 * 判据与后端**逐字节相同**（宽×高 > 4000 万）。读不出尺寸就放行——预检是省一趟往返，
 * 不是第二道安全闸；真正的把关永远在后端，前端拦不住任何蓄意请求。
 */

export const MAX_OCR_PIXELS = 40_000_000

/**
 * @returns {Promise<string>} 空串 = 可以传；非空 = 拦下来的原因（直接给用户看）
 */
export async function checkImageSize(file) {
  const wh = await readSize(file)
  if (!wh) return ''                       // 读不出来（格式怪、浏览器不支持）→ 交给后端判
  const [w, h] = wh
  if (w * h <= MAX_OCR_PIXELS) return ''
  return `「${file.name}」分辨率过大（${w}×${h}，上限 ${MAX_OCR_PIXELS / 1_000_000} 百万像素），`
    + '请裁掉无关区域或截短后重试'
}

function readSize(file) {
  return new Promise((resolve) => {
    // createImageBitmap 不解码到画布、也不受同源限制，是最省的一条路；
    // 老浏览器没有它就退回 <img>。两条路都**不会**把整张图画出来。
    if (typeof createImageBitmap === 'function') {
      createImageBitmap(file)
        .then((bmp) => { const r = [bmp.width, bmp.height]; bmp.close?.(); resolve(r) })
        .catch(() => resolve(null))
      return
    }
    const url = URL.createObjectURL(file)
    const img = new Image()
    // 成功失败都要 revokeObjectURL：一次拖十几张图不释放就是十几份原图留在内存里。
    img.onload = () => { URL.revokeObjectURL(url); resolve([img.naturalWidth, img.naturalHeight]) }
    img.onerror = () => { URL.revokeObjectURL(url); resolve(null) }
    img.src = url
  })
}
