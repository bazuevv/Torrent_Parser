package com.tonapps.tonkeeper.ui.screen.music.list

import com.tonapps.tonkeeper.ui.screen.music.entity.RadioStationEntity
import com.tonapps.uikit.list.BaseListItem
import com.tonapps.uikit.list.ListCell

sealed class Item(type: Int) : BaseListItem(type) {

    companion object {
        const val TYPE_STATION = 1
    }

    data class Station(
        override val position: ListCell.Position,
        val station: RadioStationEntity,
    ) : Item(TYPE_STATION), ListCell {

        val name: String
            get() = station.name

        val info: String?
            get() = station.info

        val logoUrl: String?
            get() = station.logoUrl
    }
}
