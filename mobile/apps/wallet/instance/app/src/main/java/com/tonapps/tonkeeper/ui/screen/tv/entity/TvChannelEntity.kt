package com.tonapps.tonkeeper.ui.screen.tv.entity

import android.os.Parcelable
import kotlinx.parcelize.Parcelize

/**
 * Канал из M3U-плейлиста. Идентификатором служит сам URL потока: `tvg-id`
 * в публичных плейлистах не обязателен и не уникален.
 */
@Parcelize
data class TvChannelEntity(
    val name: String,
    val url: String,
    val logoUrl: String?,
    val group: String?,
) : Parcelable {

    val id: String
        get() = url
}
