package com.tonapps.tonkeeper.ui.screen.tv.list.holder

import android.view.View
import android.view.ViewGroup
import androidx.appcompat.widget.AppCompatImageView
import androidx.appcompat.widget.AppCompatTextView
import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity
import com.tonapps.tonkeeper.ui.screen.tv.list.Item
import com.tonapps.tonkeeperx.R
import com.tonapps.uikit.list.BaseListHolder
import uikit.extensions.drawable
import uikit.widget.AsyncImageView

class ChannelHolder(
    parent: ViewGroup,
    private val onClick: (TvChannelEntity) -> Unit
) : BaseListHolder<Item.Channel>(parent, R.layout.view_tv_channel) {

    private val logoView = findViewById<AsyncImageView>(R.id.logo)
    private val logoPlaceholderView = findViewById<AppCompatImageView>(R.id.logo_placeholder)
    private val nameView = findViewById<AppCompatTextView>(R.id.name)
    private val groupView = findViewById<AppCompatTextView>(R.id.group)

    override fun onBind(item: Item.Channel) {
        itemView.background = item.position.drawable(context)
        itemView.setOnClickListener { onClick(item.channel) }

        nameView.text = item.name
        groupView.text = item.group
        groupView.visibility = if (item.group.isNullOrBlank()) View.GONE else View.VISIBLE

        bindLogo(item.logoUrl)
    }

    /**
     * Холдер переиспользуется, поэтому состояние сбрасывается на каждом
     * связывании — иначе логотип соседнего канала останется висеть.
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
