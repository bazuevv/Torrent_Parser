package com.tonapps.tonkeeper.ui.screen.tv.list

import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity
import com.tonapps.uikit.list.BaseListItem
import com.tonapps.uikit.list.ListCell

sealed class Item(type: Int) : BaseListItem(type) {

    companion object {
        const val TYPE_CHANNEL = 1
    }

    data class Channel(
        override val position: ListCell.Position,
        val channel: TvChannelEntity,
    ) : Item(TYPE_CHANNEL), ListCell {

        val name: String
            get() = channel.name

        val group: String?
            get() = channel.group

        val logoUrl: String?
            get() = channel.logoUrl
    }
}
