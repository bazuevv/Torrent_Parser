package com.tonapps.tonkeeper.ui.screen.music.list

import android.view.ViewGroup
import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.tonkeeper.ui.screen.music.list.holder.StationHolder
import com.tonapps.uikit.list.BaseListAdapter
import com.tonapps.uikit.list.BaseListHolder
import com.tonapps.uikit.list.BaseListItem

class Adapter(
    private val onClick: (RadioStationEntity) -> Unit
) : BaseListAdapter() {

    override fun createHolder(parent: ViewGroup, viewType: Int): BaseListHolder<out BaseListItem> {
        return when (viewType) {
            Item.TYPE_STATION -> StationHolder(parent, onClick)
            else -> throw IllegalArgumentException("Unknown view type: $viewType")
        }
    }
}
