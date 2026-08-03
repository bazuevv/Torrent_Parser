package com.tonapps.tonkeeper.ui.screen.tv.data

import android.content.Context
import androidx.core.content.edit
import com.tonapps.log.L
import com.tonapps.network.get
import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * Плейлист ТВ-каналов: качает M3U, разбирает и держит копию на диске.
 *
 * Плейлист меняется редко, а список нужен сразу при открытии вкладки, поэтому
 * свежесть проверяется по TTL, а при сетевой ошибке отдаётся просроченная
 * копия — лучше показать вчерашний список, чем пустой экран.
 */
class TvPlaylistRepository(private val context: Context) {

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val cacheFile: File
        get() = File(context.cacheDir, CACHE_FILE_NAME)

    // Загрузка идёт под замком, иначе пересоздание фрагмента вызовет второй запрос
    private val mutex = Mutex()

    @Volatile
    private var memoryCache: List<TvChannelEntity>? = null

    val playlistUrl: String
        get() = TvPlaylist.DEFAULT_URL

    suspend fun getChannels(forceRefresh: Boolean = false): List<TvChannelEntity> {
        if (!forceRefresh) {
            memoryCache?.let { return it }
        }
        return mutex.withLock {
            if (!forceRefresh) {
                memoryCache?.let { return@withLock it }
            }
            withContext(Dispatchers.IO) { loadChannels(forceRefresh) }
        }
    }

    private fun loadChannels(forceRefresh: Boolean): List<TvChannelEntity> {
        val url = playlistUrl
        if (!forceRefresh) {
            readCache(url)?.let { content ->
                val channels = M3UParser.parse(content)
                if (channels.isNotEmpty()) {
                    memoryCache = channels
                    return channels
                }
            }
        }

        val content = try {
            httpClient.get(url)
        } catch (e: Throwable) {
            L.e(e, "TV playlist download failed")
            // Сеть отвалилась — отдаём просроченную копию, если она есть
            val stale = readCache(url, ignoreTtl = true)?.let { M3UParser.parse(it) }
            if (stale.isNullOrEmpty()) {
                throw e
            }
            memoryCache = stale
            return stale
        }

        val channels = M3UParser.parse(content)
        if (channels.isNotEmpty()) {
            writeCache(url, content)
        }
        memoryCache = channels
        return channels
    }

    private fun readCache(url: String, ignoreTtl: Boolean = false): String? {
        val file = cacheFile
        if (!file.exists() || prefs.getString(KEY_CACHED_URL, null) != url) {
            return null
        }
        if (!ignoreTtl) {
            val age = System.currentTimeMillis() - prefs.getLong(KEY_CACHED_AT, 0)
            if (age > CACHE_TTL_MS || age < 0) {
                return null
            }
        }
        return try {
            file.readText()
        } catch (e: Throwable) {
            L.e(e, "TV playlist cache read failed")
            null
        }
    }

    private fun writeCache(url: String, content: String) {
        try {
            cacheFile.writeText(content)
            prefs.edit {
                putString(KEY_CACHED_URL, url)
                putLong(KEY_CACHED_AT, System.currentTimeMillis())
            }
        } catch (e: Throwable) {
            L.e(e, "TV playlist cache write failed")
        }
    }

    private companion object {

        const val CACHE_FILE_NAME = "tv_playlist.m3u"
        const val PREFS_NAME = "tv_playlist"
        const val KEY_CACHED_URL = "cached_url"
        const val KEY_CACHED_AT = "cached_at"

        val CACHE_TTL_MS = TimeUnit.HOURS.toMillis(6)
    }
}
