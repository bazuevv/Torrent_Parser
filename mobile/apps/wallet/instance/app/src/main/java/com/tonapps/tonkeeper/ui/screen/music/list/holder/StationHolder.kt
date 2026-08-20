package com.tonapps.tonkeeper.ui.screen.music.list.holder

import android.view.View
import android.view.ViewGroup
import androidx.appcompat.widget.AppCompatImageView
import androidx.appcompat.widget.AppCompatTextView
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.tonkeeper.ui.screen.music.list.Item
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.list.BaseListHolder
import uikit.extensions.drawable
import uikit.widget.AsyncImageView

class StationHolder(
    parent: ViewGroup,
    private val onClick: (RadioStationEntity) -> Unit
) : BaseListHolder<Item.Station>(parent, R.layout.view_radio_station) {

    private val logoView = findViewById<AsyncImageView>(R.id.logo)
    private val logoPlaceholderView = findViewById<AppCompatImageView>(R.id.logo_placeholder)
    private val nameView = findViewById<AppCompatTextView>(R.id.name)
    private val infoView = findViewById<AppCompatTextView>(R.id.info)

    override fun onBind(item: Item.Station) {
        itemView.background = item.position.drawable(context)
        itemView.setOnClickListener { onClick(item.station) }

        nameView.text = item.name
        infoView.text = item.info
        infoView.visibility = if (item.info.isNullOrBlank()) View.GONE else View.VISIBLE

        bindLogo(item.logoUrl)
    }

    /**
     * Холдер переиспользуется, поэтому состояние сбрасывается на каждом
     * связывании — иначе логотип соседней станции останется висеть.
     */
    private fun bindLogo(url: String?) {
        if (url.isNullOrBlank()) {
            logoView.visibility = View.GONE
            logoPlaceholderView.visibility = View.VISIBLE
            return
        }
        logoView.visibility = View.VISIBLE
        logoPlaceholderView.visibility = View.GONE
        logoView.setImageURI(url, null)
    }
}
