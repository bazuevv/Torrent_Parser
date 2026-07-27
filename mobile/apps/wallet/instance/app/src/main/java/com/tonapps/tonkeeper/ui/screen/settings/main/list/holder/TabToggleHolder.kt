package com.tonapps.tonkeeper.ui.screen.settings.main.list.holder

import android.view.ViewGroup
import androidx.appcompat.widget.AppCompatTextView
import com.tonapps.tonkeeper.ui.screen.settings.main.list.Item
import com.tonapps.tonkeeperx.R
import uikit.extensions.drawable
import uikit.widget.SwitchView

class TabToggleHolder(
    parent: ViewGroup,
    onClick: ((Item) -> Unit)
) : Holder<Item.TabToggle>(parent, R.layout.view_tab_toggle, onClick) {

    private val titleView = findViewById<AppCompatTextView>(R.id.title)
    private val descriptionView = findViewById<AppCompatTextView>(R.id.description)
    private val switchView = findViewById<SwitchView>(R.id.toggle)

    override fun onBind(item: Item.TabToggle) {
        itemView.background = item.position.drawable(context)

        titleView.setText(item.tab.titleRes)
        descriptionView.setText(item.tab.descriptionRes)

        // Сначала снимаем прошлый обработчик: холдер переиспользуется, и установка
        // состояния чужого элемента иначе улетела бы в onClick как действие пользователя
        switchView.doCheckedChanged = null
        switchView.setChecked(item.enabled, false)

        switchView.doCheckedChanged = { _, byUser ->
            if (byUser) {
                onClick(item)
            }
        }
    }
}
