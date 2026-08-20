package com.tonapps.tonkeeper.ui.screen.music

import com.tonapps.uikit.list.BaseListItem

sealed class MusicUiState {

    data object Loading : MusicUiState()

    data object Empty : MusicUiState()

    data object Error : MusicUiState()

    data class Items(val items: List<BaseListItem>) : MusicUiState()
}
