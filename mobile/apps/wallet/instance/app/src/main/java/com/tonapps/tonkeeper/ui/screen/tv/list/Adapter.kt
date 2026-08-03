package com.tonapps.tonkeeper.ui.screen.tv.list

import android.view.ViewGroup
import com.tonapps.tonkeeper.ui.screen.tv.entity.TvChannelEntity
import com.tonapps.tonkeeper.ui.screen.tv.list.holder.ChannelHolder
import com.tonapps.uikit.list.BaseListAdapter
import com.tonapps.uikit.list.BaseListHolder
import com.tonapps.uikit.list.BaseListItem

class Adapter(
    private val onClick: (TvChannelEntity) -> Unit
) : BaseListAdapter() {

    override fun createHolder(parent: ViewGroup, viewType: Int): BaseListHolder<out BaseListItem> {
        return when (viewType) {
            Item.TYPE_CHANNEL -> ChannelHolder(parent, onClick)
            else -> throw IllegalArgumentException("Unknown view type: $viewType")
        }
    }
}
