package com.tonapps.tonkeeper.ui.screen.send.contacts.main.list.holder

import android.view.View
import android.view.ViewGroup
import androidx.appcompat.widget.AppCompatImageView
import androidx.appcompat.widget.AppCompatTextView
import androidx.core.net.toUri
import com.tonapps.emoji.ui.EmojiView
import com.tonapps.tonkeeper.ui.screen.send.contacts.main.list.Item
import com.tonapps.tonkeeperx.R
import uikit.widget.AsyncImageView
import java.io.File

abstract class ContactHolder<I: Item>(parent: ViewGroup): Holder<I>(parent, R.layout.view_contact) {

    companion object {
        const val EDIT_ID = 1L
        const val DELETE_ID = 2L
        const val ADD_TO_CONTACTS_ID = 3L
        const val HIDE_ID = 4L
    }

    val emojiView = itemView.findViewById<EmojiView>(R.id.emoji)
    val photoView = itemView.findViewById<AsyncImageView>(R.id.photo)
    val nameView = itemView.findViewById<AppCompatTextView>(R.id.name)
    val iconView = itemView.findViewById<AppCompatImageView>(R.id.icon)

    /**
     * Фото показывается вместо эмодзи-заглушки. Холдер переиспользуется, поэтому
     * состояние сбрасывается на каждом связывании, иначе чужой аватар останется висеть.
     */
    fun bindPhoto(path: String?) {
        if (path.isNullOrBlank()) {
            photoView.visibility = View.GONE
            emojiView.visibility = View.VISIBLE
            return
        }
        photoView.visibility = View.VISIBLE
        emojiView.visibility = View.GONE
        photoView.setImageURI(File(path).toUri())
    }

}