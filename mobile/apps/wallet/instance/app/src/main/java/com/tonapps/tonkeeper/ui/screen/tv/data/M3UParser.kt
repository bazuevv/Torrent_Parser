package com.tonapps.tonkeeper.ui.screen.tv.data

import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity

/**
 * Разбор M3U/M3U8-плейлиста вида:
 *
 *     #EXTINF:-1 tvg-logo="https://…" group-title="General",Первый канал
 *     https://example.com/stream.m3u8
 *
 * Директивы, кроме `#EXTINF` и `#EXTGRP`, пропускаются: в публичных плейлистах
 * между описанием и ссылкой часто стоят `#EXTVLCOPT`, `#KODIPROP` и подобные.
 */
object M3UParser {

    private val ATTRIBUTE_REGEX = Regex("""([\w-]+)="([^"]*)"""")

    fun parse(content: String): List<TvChannelEntity> {
        val channels = mutableListOf<TvChannelEntity>()
        val seenUrls = mutableSetOf<String>()

        var pendingName: String? = null
        var pendingLogo: String? = null
        var pendingGroup: String? = null

        for (rawLine in content.lineSequence()) {
            val line = rawLine.trim()
            if (line.isEmpty()) {
                continue
            }

            when {
                line.startsWith("#EXTINF", ignoreCase = true) -> {
                    val attributes = parseAttributes(line)
                    pendingName = parseTitle(line).ifBlank { attributes["tvg-name"] ?: "" }
                    pendingLogo = attributes["tvg-logo"]?.ifBlank { null }
                    pendingGroup = attributes["group-title"]?.ifBlank { null }
                }
                line.startsWith("#EXTGRP:", ignoreCase = true) -> {
                    pendingGroup = line.substringAfter(':').trim().ifBlank { null }
                }
                line.startsWith("#") -> continue
                else -> {
                    val name = pendingName
                    pendingName = null
                    if (name.isNullOrBlank() || !isStreamUrl(line) || !seenUrls.add(line)) {
                        continue
                    }
                    channels.add(
                        TvChannelEntity(
                            name = name,
                            url = line,
                            logoUrl = pendingLogo,
                            group = pendingGroup,
                        )
                    )
                }
            }
        }
        return channels
    }

    private fun parseAttributes(line: String): Map<String, String> {
        return ATTRIBUTE_REGEX.findAll(line).associate { match ->
            match.groupValues[1].lowercase() to match.groupValues[2]
        }
    }

    /**
     * Название идёт после запятой, следующей за списком атрибутов. Считать от
     * первой запятой нельзя — запятая встречается внутри `group-title="A, B"`.
     */
    private fun parseTitle(line: String): String {
        val lastQuote = line.lastIndexOf('"')
        val commaIndex = if (lastQuote == -1) {
            line.indexOf(',')
        } else {
            line.indexOf(',', lastQuote)
        }
        if (commaIndex == -1) {
            return ""
        }
        return line.substring(commaIndex + 1).trim()
    }

    private fun isStreamUrl(line: String): Boolean {
        return line.startsWith("http://", ignoreCase = true) ||
            line.startsWith("https://", ignoreCase = true)
    }
}
