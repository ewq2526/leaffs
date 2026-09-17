package com.leaffs.mobile

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.MediaMetadataRetriever
import java.io.File
import java.io.FileOutputStream
import kotlin.math.max
import kotlin.math.roundToInt

/**
 * 缩略图生成 —— 安卓没有 ffmpeg，改用系统 API。
 *
 * 由 Python 侧 leaffs.utils.core 的 set_native_thumbnailer 注册后调用（启动时注册）。
 * 这里只负责「把源文件变成一张 JPEG」：宽 256、JPEG，与桌面 ffmpeg 那条路输出一致；
 * 缓存、索引、失效全部复用 Python 现成的机制（_thumb_path / _thumb_index_add /
 * get_thumbnail 的 mtime 比对），本类不参与。
 */
class Thumbnailer {

    /** Python 侧入口：srcPath 是共享目录里的源文件，dstPath 是缩略图目标路径 */
    fun generate(srcPath: String, dstPath: String): Boolean {
        return try {
            val ext = srcPath.substringAfterLast('.', "").lowercase()
            val src = if (VIDEO_EXTS.contains(ext)) videoFrame(srcPath) else decodeImage(srcPath)
            if (src == null) {
                android.util.Log.w("LeafFS", "缩略图源解码失败: $srcPath")
                return false
            }
            val out = File(dstPath)
            out.parentFile?.mkdirs()
            FileOutputStream(out).use {
                scaleToWidth(src, THUMB_WIDTH).compress(Bitmap.CompressFormat.JPEG, 85, it)
            }
            out.length() > 0
        } catch (t: Throwable) {
            android.util.Log.w("LeafFS", "缩略图生成失败: $srcPath", t)
            false
        }
    }

    /** 图片：先读尺寸按 2 的幂降采样，避免整张原图进内存（手机照片动辄 4000+ 宽） */
    private fun decodeImage(path: String): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(path, bounds)
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        var sample = 1
        while (bounds.outWidth / (sample * 2) >= THUMB_WIDTH) sample *= 2
        val opts = BitmapFactory.Options().apply { inSampleSize = sample }
        return BitmapFactory.decodeFile(path, opts)
    }

    /** 视频：取第 1 秒那一帧（与 ffmpeg 的 -ss 00:00:01 对齐），取不到就退回第一帧 */
    private fun videoFrame(path: String): Bitmap? {
        val mmr = MediaMetadataRetriever()
        return try {
            mmr.setDataSource(path)
            mmr.getFrameAtTime(1_000_000L, MediaMetadataRetriever.OPTION_CLOSEST_SYNC)
                ?: mmr.frameAtTime
        } catch (t: Throwable) {
            null
        } finally {
            try {
                mmr.release()
            } catch (_: Throwable) {
            }
        }
    }

    /** 等比缩到指定宽度；本来就不宽就原样返回 */
    private fun scaleToWidth(src: Bitmap, width: Int): Bitmap {
        if (src.width <= width) return src
        val h = max(1, (src.height.toFloat() * width / src.width).roundToInt())
        return Bitmap.createScaledBitmap(src, width, h, true)
    }

    private companion object {
        const val THUMB_WIDTH = 256

        /** 与 Python 侧 _generate_thumbnail 判断视频的扩展名列表保持一致 */
        val VIDEO_EXTS = setOf(
            "mp4", "webm", "mov", "avi", "mkv", "flv", "ts", "mts",
            "m4v", "3gp", "ogv", "wmv", "vob", "mpeg", "mpg"
        )
    }
}
