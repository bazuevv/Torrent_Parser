package com.tonapps.wallet.api.entity

import android.os.Parcelable
import kotlinx.parcelize.Parcelize
import org.json.JSONArray
import org.json.JSONObject

@Parcelize
data class BannerEntity(
    val id: String,
    val title: String,
    val description: String,
    val image: String?,
    val textColor: String?,
    val backgroundColor: String?,
    val button: Button?
): Parcelable {

    @Parcelize
    data class Button(
        val type: Type,
        val payload: String,
        val title: String
    ): Parcelable {

        enum class Type {
            LINK,
            DEEPLINK;

            companion object {

                fun of(value: String): Type {
                    return when (value.lowercase()) {
                        "deeplink" -> DEEPLINK
                        else -> LINK
                    }
                }
            }
        }

        constructor(json: JSONObject) : this(
            type = Type.of(json.getString("type")),
            payload = json.getString("payload"),
            title = json.getString("title")
        )
    }

    constructor(json: JSONObject) : this(
        id = json.getString("id"),
        title = json.getString("title"),
        description = json.getString("description"),
        image = json.optStringOrNull("image"),
        textColor = json.optStringOrNull("textColor"),
        backgroundColor = json.optStringOrNull("backgroundColor"),
        button = json.optJSONObject("button")?.let { Button(it) }
    )

    companion object {

        fun parse(array: JSONArray): List<BannerEntity> {
            return (0 until array.length()).map { BannerEntity(array.getJSONObject(it)) }
        }

        private fun JSONObject.optStringOrNull(name: String): String? {
            if (!has(name) || isNull(name)) {
                return null
            }
            return optString(name).takeIf { it.isNotBlank() }
        }
    }
}
