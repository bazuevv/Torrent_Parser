package com.tonapps.tonkeeper.ui.screen.music.data

import android.content.Context
import androidx.core.content.edit
import com.tonapps.log.L
import com.tonapps.network.get
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import org.json.JSONArray
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Каталог радиостанций Radio-Browser: качает JSON по одному из зеркал,
 * разбирает и держит копию на диске. Схема та же, что у ТВ-плейлиста:
 * TTL на свежесть, а при сетевой ошибке отдаётся просроченная копия.
 */
class RadioBrowserRepository(private val context: Context) {

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(20, TimeUnit.SECONDS)
        .build()

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val cacheFile: File
        get() = File(context.cacheDir, CACHE_FILE_NAME)

    // Загрузка идёт под замком, иначе пересоздание фрагмента вызовет второй запрос
    private val mutex = Mutex()

    @Volatile
    private var memoryCache: List<RadioStationEntity>? = null

    suspend fun getStations(forceRefresh: Boolean = false): List<RadioStationEntity> {
        if (!forceRefresh) {
            memoryCache?.let { return it }
        }
        return mutex.withLock {
            if (!forceRefresh) {
                memoryCache?.let { return@withLock it }
            }
            withContext(Dispatchers.IO) { loadStations(forceRefresh) }
        }
    }

    private fun loadStations(forceRefresh: Boolean): List<RadioStationEntity> {
        if (!forceRefresh) {
            readCache()?.let { raw ->
                parseStations(raw)?.let { stations ->
                    if (stations.isNotEmpty()) {
                        memoryCache = stations
                        return stations
                    }
                }
            }
        }

        val raw = try {
            download() ?: run {
                // Все зеркала промолчали — отдаём просроченную копию, если она есть
                val stale = readCache(ignoreTtl = true)?.let { parseStations(it) }
                if (stale.isNullOrEmpty()) {
                    throw IOException("All Radio-Browser mirrors failed")
                }
                memoryCache = stale
                return stale
            }
        } catch (e: Throwable) {
            L.e(e, "Radio stations download failed")
            throw e
        }

        val stations = parseStations(raw).orEmpty()
        if (stations.isNotEmpty()) {
            writeCache(raw)
        }
        memoryCache = stations
        return stations
    }

    private fun download(): String? {
        for (host in RadioBrowser.MIRRORS) {
            try {
                return httpClient.get("https://$host${RadioBrowser.STATIONS_PATH}")
            } catch (e: Throwable) {
                L.w("Radio-Browser mirror $host failed: ${e.message}")
            }
        }
        return null
    }

    /**
     * Станции по `http://` отбрасываются: `network_security_config.xml` разрешает
     * открытый текст только для `*.ton`. Логотипы по http просто не загрузятся
     * и останутся заглушкой — это не повод резать станцию из списка.
     */
    private fun parseStations(raw: String?): List<RadioStationEntity>? {
        if (raw.isNullOrBlank()) {
            return null
        }
        return try {
            val array = JSONArray(raw)
            val stations = mutableListOf<RadioStationEntity>()
            val seenUrls = mutableSetOf<String>()
            for (i in 0 until array.length()) {
                val obj = array.optJSONObject(i) ?: continue
                val name = obj.optString("name").trim()
                val url = obj.optString("url_resolved").trim()
                if (name.isEmpty() || !url.startsWith("https://", ignoreCase = true)) {
                    continue
                }
                if (!seenUrls.add(url)) {
                    continue
                }
                stations.add(
                    RadioStationEntity(
                        name = name,
                        url = url,
                        logoUrl = obj.optString("favicon").trim().ifBlank { null },
                        codec = obj.optString("codec").trim().ifBlank { null },
                        bitrate = obj.optInt("bitrate", 0),
                    )
                )
            }
            stations
        } catch (e: Throwable) {
            L.e(e, "Radio stations parse failed")
            null
        }
    }

    private fun readCache(ignoreTtl: Boolean = false): String? {
        val file = cacheFile
        if (!file.exists()) {
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
            L.e(e, "Radio stations cache read failed")
            null
        }
    }

    private fun writeCache(content: String) {
        try {
            cacheFile.writeText(content)
            prefs.edit { putLong(KEY_CACHED_AT, System.currentTimeMillis()) }
        } catch (e: Throwable) {
            L.e(e, "Radio stations cache write failed")
        }
    }

    private companion object {

        const val CACHE_FILE_NAME = "radio_stations.json"
        const val PREFS_NAME = "radio_browser"
        const val KEY_CACHED_AT = "cached_at"

        val CACHE_TTL_MS = TimeUnit.HOURS.toMillis(6)
    }
}
