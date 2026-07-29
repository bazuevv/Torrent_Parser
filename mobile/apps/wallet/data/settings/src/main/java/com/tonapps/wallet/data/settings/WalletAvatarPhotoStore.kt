package com.tonapps.wallet.data.settings

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import com.tonapps.log.L
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.io.FileOutputStream

/**
 * Файлы фотографий, выбранных пользователем на аватар кошелька.
 *
 * Изображение из галереи копируется в приватную папку приложения: `content://`-ссылка
 * живёт только пока действует выданный пикером доступ, и хранить её как долговременный
 * путь нельзя. При копировании картинка ужимается до [MAX_SIZE] — на аватаре 64 dp
 * оригинал в несколько мегапикселей не нужен и грозит OOM при декодировании.
 *
 * Имя файла содержит метку времени, поэтому каждое сохранение даёт новый путь.
 * Это заодно решает вопрос устаревших кэшей изображений: путь меняется — кэш
 * промахивается и картинка перечитывается.
 */
object WalletAvatarPhotoStore {

    const val OUTPUT_SIZE = 512

    private const val DIR_NAME = "wallet_avatars"
    private const val MAX_SIZE = OUTPUT_SIZE
    private const val QUALITY = 90

    /** Читает выбранное изображение, ужимая его при декодировании. */
    suspend fun decode(context: Context, uri: Uri): Bitmap? {
        return withContext(Dispatchers.IO) {
            try {
                decodeScaled(context, uri)
            } catch (e: Throwable) {
                L.e(e)
                null
            }
        }
    }

    suspend fun save(context: Context, walletId: String, bitmap: Bitmap): File? {
        return withContext(Dispatchers.IO) {
            try {
                val file = File(dir(context), "${walletId}_${System.currentTimeMillis()}.jpg")
                FileOutputStream(file).use { output ->
                    bitmap.compress(Bitmap.CompressFormat.JPEG, QUALITY, output)
                }
                file
            } catch (e: Throwable) {
                L.e(e)
                null
            }
        }
    }

    fun delete(path: String?) {
        val file = path?.let { File(it) } ?: return
        if (file.exists()) {
            file.delete()
        }
    }

    private fun dir(context: Context): File {
        val dir = File(context.filesDir, DIR_NAME)
        if (!dir.exists()) {
            dir.mkdirs()
        }
        return dir
    }

    /**
     * Читает изображение, уменьшая его при декодировании: сначала узнаём размеры без
     * выделения памяти под пиксели, затем декодируем с подходящим inSampleSize.
     */
    private fun decodeScaled(context: Context, uri: Uri): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri)?.use { input ->
            BitmapFactory.decodeStream(input, null, bounds)
        }
        val largestSide = maxOf(bounds.outWidth, bounds.outHeight)
        if (largestSide <= 0) {
            return null
        }
        val options = BitmapFactory.Options().apply {
            inSampleSize = sampleSize(largestSide)
        }
        return context.contentResolver.openInputStream(uri)?.use { input ->
            BitmapFactory.decodeStream(input, null, options)
        }
    }

    private fun sampleSize(largestSide: Int): Int {
        var sample = 1
        while (largestSide / (sample * 2) >= MAX_SIZE) {
            sample *= 2
        }
        return sample
    }
}
