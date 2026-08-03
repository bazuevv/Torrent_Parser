package com.tonapps.tonkeeper.ui.screen.tv

import com.tonapps.uikit.list.BaseListItem

sealed class TvUiState {

    data object Loading : TvUiState()

    data object Empty : TvUiState()

    data object Error : TvUiState()

    data class Items(val items: List<BaseListItem>) : TvUiState()
}
