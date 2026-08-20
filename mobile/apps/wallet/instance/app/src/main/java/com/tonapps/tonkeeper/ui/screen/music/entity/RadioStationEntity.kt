package com.tonapps.tonkeeper.ui.screen.music.entity

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * Радиостанция из каталога Radio-Browser. Идентификатором служит сам URL
 * потока: stationuuid не нужен, станция в списке встречается один раз.
 */
@Parcelize
data class RadioStationEntity(
    val name: String,
    val url: String,
    val logoUrl: String?,
    val codec: String?,
    val bitrate: Int,
) : Parcelable {

    val id: String
        get() = url

    /** Вторая строка списка: «MP3 · 128 kbps» или просто кодек */
    val info: String?
        get() = when {
            codec.isNullOrBlank() && bitrate <= 0 -> null
            codec.isNullOrBlank() -> "${bitrate} kbps"
            bitrate <= 0 -> codec
            else -> "$codec · $bitrate kbps"
        }
}
