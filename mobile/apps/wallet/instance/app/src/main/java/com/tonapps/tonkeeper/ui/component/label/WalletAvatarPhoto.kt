package com.tonapps.tonkeeper.ui.component.label

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache
import android.view.View
import android.widget.ImageView

private val cache = object : LruCache<String, Bitmap>(CACHE_SIZE) {
    override fun sizeOf(key: String, value: Bitmap) = 1
}

private const val CACHE_SIZE = 8

/**
 * Ставит фотографию кошелька в [photoView], пряча [emojiView], либо возвращает
 * эмодзи, если фотографии нет.
 *
 * Фотография лежит с отступом внутри подложки, поэтому цвет метки виден вокруг
 * неё кольцом — так же, как в редакторе.
 *
 * Снимки хранятся уже ужатыми до 512 px, поэтому читаются синхронно: на списке
 * из нескольких кошельков это единицы миллисекунд. От повторного чтения при
 * каждом связывании ячейки спасает кэш — путь к файлу меняется при каждой
 * замене фотографии, так что устаревшие записи не всплывают.
 */
fun applyWalletPhoto(emojiView: View, photoView: ImageView, photoPath: String?) {
    val bitmap = photoPath?.takeIf { it.isNotBlank() }?.let { path ->
        cache.get(path) ?: BitmapFactory.decodeFile(path)?.also { cache.put(path, it) }
    }
    if (bitmap == null) {
        photoView.setImageDrawable(null)
        photoView.visibility = View.GONE
        emojiView.visibility = View.VISIBLE
    } else {
        photoView.setImageBitmap(bitmap)
        photoView.clipToOutline = true
        photoView.visibility = View.VISIBLE
        emojiView.visibility = View.GONE
    }
}
