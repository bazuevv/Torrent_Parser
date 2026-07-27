package com.tonapps.wallet.data.banner.entities

import android.os.Parcelable
import com.tonapps.wallet.api.entity.BannerEntity
import kotlinx.parcelize.Parcelize

@Parcelize
data class BannerDataEntity(
    val banners: List<BannerEntity>
): Parcelable
