package com.tonapps.wallet.data.contacts.source

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import com.tonapps.extensions.currentTimeSeconds
import com.tonapps.sqlite.SQLiteHelper
import com.tonapps.wallet.data.contacts.entities.ContactEntity

internal class DatabaseSource(
    context: Context
): SQLiteHelper(context, DATABASE_NAME, DATABASE_VERSION) {

    private companion object {
        private const val DATABASE_NAME = "contacts"
        private const val DATABASE_VERSION = 3

        private const val CONTACTS_TABLE = "contacts"
        private const val CONTACTS_ID = "_id"
        private const val CONTACTS_NAME = "name"
        private const val CONTACTS_ADDRESS = "address"
        private const val CONTACTS_DATE = "date"
        private const val CONTACTS_TESTNET = "testnet"

        /**
         * LOOKUP_KEY контакта телефонной книги, а не _ID: последний меняется при
         * пересоздании контакта, восстановлении из бэкапа и слиянии дубликатов.
         */
        private const val CONTACTS_LOOKUP_KEY = "lookup_key"

        /**
         * Путь к копии фото во внутреннем хранилище. Ссылку на телефонную книгу
         * хранить нельзя: доступ к ней действует только на время выбора контакта.
         */
        private const val CONTACTS_PHOTO_PATH = "photo_path"

        private val contactsField = arrayOf(
            CONTACTS_ID,
            CONTACTS_NAME,
            CONTACTS_ADDRESS,
            CONTACTS_DATE,
            CONTACTS_TESTNET,
            CONTACTS_LOOKUP_KEY,
            CONTACTS_PHOTO_PATH
        ).joinToString(",")

        private const val KEY_HIDDEN = "hidden"
    }

    private val prefs = context.getSharedPreferences("contacts", Context.MODE_PRIVATE)

    override fun create(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE $CONTACTS_TABLE (" +
                "$CONTACTS_ID INTEGER PRIMARY KEY AUTOINCREMENT," +
                "$CONTACTS_NAME TEXT NOT NULL," +
                "$CONTACTS_ADDRESS TEXT NOT NULL," +
                "$CONTACTS_DATE INTEGER NOT NULL," +
                "$CONTACTS_TESTNET INTEGER NOT NULL DEFAULT 0," +
                "$CONTACTS_LOOKUP_KEY TEXT," +
                "$CONTACTS_PHOTO_PATH TEXT" +
                ")")
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        super.onUpgrade(db, oldVersion, newVersion)
        // Шаги последовательные, а не парой old→new: база могла остаться на версии 1,
        // и тогда при обновлении сразу до 3 колонка testnet иначе не добавилась бы
        if (oldVersion < 2) {
            db.execSQL("ALTER TABLE $CONTACTS_TABLE ADD COLUMN $CONTACTS_TESTNET INTEGER NOT NULL DEFAULT 0")
        }
        if (oldVersion < 3) {
            db.execSQL("ALTER TABLE $CONTACTS_TABLE ADD COLUMN $CONTACTS_LOOKUP_KEY TEXT")
            db.execSQL("ALTER TABLE $CONTACTS_TABLE ADD COLUMN $CONTACTS_PHOTO_PATH TEXT")
        }
    }

    fun getContacts(): List<ContactEntity> {
        val contacts = mutableListOf<ContactEntity>()
        val query = "SELECT $contactsField FROM $CONTACTS_TABLE LIMIT 100"
        val cursor = readableDatabase.rawQuery(query, null)
        val idIndex = cursor.getColumnIndex(CONTACTS_ID)
        val nameIndex = cursor.getColumnIndex(CONTACTS_NAME)
        val addressIndex = cursor.getColumnIndex(CONTACTS_ADDRESS)
        val dateIndex = cursor.getColumnIndex(CONTACTS_DATE)
        val testnetIndex = cursor.getColumnIndex(CONTACTS_TESTNET)
        val lookupKeyIndex = cursor.getColumnIndex(CONTACTS_LOOKUP_KEY)
        val photoPathIndex = cursor.getColumnIndex(CONTACTS_PHOTO_PATH)
        while (cursor.moveToNext()) {
            contacts.add(ContactEntity(
                id = cursor.getLong(idIndex),
                name = cursor.getString(nameIndex),
                address = cursor.getString(addressIndex),
                date = cursor.getLong(dateIndex),
                testnet = cursor.getLong(testnetIndex) != 0L,
                lookupKey = if (lookupKeyIndex == -1) {
                    null
                } else {
                    cursor.getString(lookupKeyIndex)
                },
                photoPath = if (photoPathIndex == -1) {
                    null
                } else {
                    cursor.getString(photoPathIndex)
                }
            ))
        }
        cursor.close()
        return contacts
    }

    fun addContact(
        name: String,
        address: String,
        testnet: Boolean,
        lookupKey: String? = null,
        photoPath: String? = null
    ): ContactEntity {
        val date = currentTimeSeconds()
        val values = ContentValues().apply {
            put(CONTACTS_NAME, name)
            put(CONTACTS_ADDRESS, address)
            put(CONTACTS_DATE, date)
            put(CONTACTS_TESTNET, if (testnet) 1L else 0L)
            put(CONTACTS_LOOKUP_KEY, lookupKey)
            put(CONTACTS_PHOTO_PATH, photoPath)
        }
        val id = writableDatabase.insert(CONTACTS_TABLE, null, values)
        return ContactEntity(id, name, address, date, testnet, lookupKey, photoPath)
    }

    fun editContact(id: Long, name: String) {
        val values = ContentValues().apply {
            put(CONTACTS_NAME, name)
        }
        writableDatabase.update(CONTACTS_TABLE, values, "$CONTACTS_ID = ?", arrayOf(id.toString()))
    }

    fun deleteContact(id: Long) {
        writableDatabase.delete(CONTACTS_TABLE, "$CONTACTS_ID = ?", arrayOf(id.toString()))
    }

    private fun prefixHidden(accountId: String, testnet: Boolean): String {
        return "$KEY_HIDDEN:$accountId:${if (testnet) "testnet" else "mainnet"}"
    }

    fun isHidden(accountId: String, testnet: Boolean): Boolean {
        return prefs.getBoolean(prefixHidden(accountId, testnet), false)
    }

    fun setHidden(accountId: String, testnet: Boolean, hidden: Boolean) {
        prefs.edit().putBoolean(prefixHidden(accountId, testnet), hidden).apply()
    }
}