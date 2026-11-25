//! A minimal ZIP writer: stored (uncompressed) entries, no dependencies.
//!
//! `diagnostics` writes a bundle the operator is expected to open and read, so
//! the container has to be a real ZIP. The workspace has no compression crate
//! and adding one (`zip` pulls flate2, zopfli and a dozen transitive crates)
//! would be a dependency decision this command does not need: the bundle is a
//! JSON document plus a README, and a stored entry is a valid ZIP that every
//! reader accepts.
//!
//! What is written is the minimum a reader needs — local file headers, a
//! central directory and the end-of-central-directory record — with a fixed
//! DOS timestamp (1980-01-01) so two runs of the same content produce the same
//! bytes. The CRC-32 is real: a corrupted entry fails the reader's check
//! instead of being silently served.

use std::io::{self, Write};
use std::path::Path;

/// DOS timestamp for 1980-01-01 00:00:00, the ZIP epoch.
const DOS_TIME: u16 = 0;
const DOS_DATE: u16 = 0x0021;

/// General-purpose flag bit 11: the file name is UTF-8.
const FLAG_UTF8: u16 = 0x0800;

/// Write `entries` to `path` as a ZIP archive.
pub fn write_zip(path: &Path, entries: &[(&str, &[u8])]) -> io::Result<()> {
    let mut out: Vec<u8> = Vec::new();
    let mut directory: Vec<(String, u32, u32, u32)> = Vec::with_capacity(entries.len());

    for (name, data) in entries {
        let offset = out.len() as u32;
        let crc = crc32(data);
        let size = data.len() as u32;
        let name_bytes = name.as_bytes();

        write_u32(&mut out, 0x0403_4b50); // local file header signature
        write_u16(&mut out, 20); // version needed to extract
        write_u16(&mut out, FLAG_UTF8);
        write_u16(&mut out, 0); // method: stored
        write_u16(&mut out, DOS_TIME);
        write_u16(&mut out, DOS_DATE);
        write_u32(&mut out, crc);
        write_u32(&mut out, size); // compressed size
        write_u32(&mut out, size); // uncompressed size
        write_u16(&mut out, name_bytes.len() as u16);
        write_u16(&mut out, 0); // extra field length
        out.extend_from_slice(name_bytes);
        out.extend_from_slice(data);

        directory.push(((*name).to_string(), crc, size, offset));
    }

    let directory_offset = out.len() as u32;
    for (name, crc, size, offset) in &directory {
        let name_bytes = name.as_bytes();
        write_u32(&mut out, 0x0201_4b50); // central directory header signature
        write_u16(&mut out, 20); // version made by
        write_u16(&mut out, 20); // version needed to extract
        write_u16(&mut out, FLAG_UTF8);
        write_u16(&mut out, 0); // method: stored
        write_u16(&mut out, DOS_TIME);
        write_u16(&mut out, DOS_DATE);
        write_u32(&mut out, *crc);
        write_u32(&mut out, *size);
        write_u32(&mut out, *size);
        write_u16(&mut out, name_bytes.len() as u16);
        write_u16(&mut out, 0); // extra field length
        write_u16(&mut out, 0); // comment length
        write_u16(&mut out, 0); // disk number start
        write_u16(&mut out, 0); // internal attributes
        write_u32(&mut out, 0); // external attributes
        write_u32(&mut out, *offset);
        out.extend_from_slice(name_bytes);
    }
    let directory_size = out.len() as u32 - directory_offset;

    write_u32(&mut out, 0x0605_4b50); // end of central directory signature
    write_u16(&mut out, 0); // this disk
    write_u16(&mut out, 0); // disk with the central directory
    write_u16(&mut out, directory.len() as u16);
    write_u16(&mut out, directory.len() as u16);
    write_u32(&mut out, directory_size);
    write_u32(&mut out, directory_offset);
    write_u16(&mut out, 0); // comment length

    let mut file = std::fs::File::create(path)?;
    file.write_all(&out)?;
    file.flush()
}

fn write_u16(out: &mut Vec<u8>, value: u16) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn write_u32(out: &mut Vec<u8>, value: u32) {
    out.extend_from_slice(&value.to_le_bytes());
}

/// CRC-32 (IEEE 802.3), the checksum ZIP entries carry.
pub fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for byte in data {
        crc ^= u32::from(*byte);
        for _ in 0..8 {
            let mask = (crc & 1).wrapping_neg();
            crc = (crc >> 1) ^ (0xedb8_8320 & mask);
        }
    }
    !crc
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_matches_the_reference_values() {
        assert_eq!(crc32(b""), 0);
        assert_eq!(crc32(b"123456789"), 0xcbf4_3926);
        assert_eq!(
            crc32(b"The quick brown fox jumps over the lazy dog"),
            0x414f_a339
        );
    }

    #[test]
    fn a_written_archive_has_the_expected_structure() {
        let dir = std::env::temp_dir().join(format!("xpst-zipwrite-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("scratch dir");
        let path = dir.join("bundle.zip");
        write_zip(
            &path,
            &[
                ("diagnostics.json", b"{\"a\": 1}\n"),
                ("README.txt", b"hello\n"),
            ],
        )
        .expect("write zip");

        let bytes = std::fs::read(&path).expect("read zip");
        assert_eq!(&bytes[0..4], b"PK\x03\x04");
        // Two local headers, two central directory entries, one EOCD.
        assert_eq!(
            bytes
                .windows(4)
                .filter(|window| *window == b"PK\x03\x04")
                .count(),
            2
        );
        assert_eq!(
            bytes
                .windows(4)
                .filter(|window| *window == b"PK\x01\x02")
                .count(),
            2
        );
        assert_eq!(
            bytes
                .windows(4)
                .filter(|window| *window == b"PK\x05\x06")
                .count(),
            1
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
