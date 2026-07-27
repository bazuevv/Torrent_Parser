package com.tonapps.wallet.data.banner.source

import android.content.Context
import com.tonapps.extensions.toByteArray
import com.tonapps.extensions.toParcel
import com.tonapps.wallet.data.banner.entities.BannerDataEntity
import com.tonapps.wallet.data.core.BlobDataSource

internal class LocalDataSource(context: Context): BlobDataSource<BannerDataEntity>(
    context = context,
    path = "banner_data"
) {
    override fun onMarshall(data: BannerDataEntity) = data.toByteArray()

    override fun onUnmarshall(bytes: ByteArray) = bytes.toParcel<BannerDataEntity>()
}
