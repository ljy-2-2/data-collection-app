//! Utilities for efficient reading of ROS bag files.
//!
//! # Example
//! ```
//! use rosbag::{ChunkRecord, MessageRecord, IndexRecord, RosBag};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! # let path = "dummy.bag";
//! let bag = RosBag::new(path)?;
//! // Iterate over records in the chunk section
//! for record in bag.chunk_records() {
//!     match record? {
//!         ChunkRecord::Chunk(chunk) => {
//!             // iterate over messages in the chunk
//!             for msg in chunk.messages() {
//!                 match msg? {
//!                     MessageRecord::MessageData(msg_data) => {
//!                         // ..
//!                         # drop(msg_data);
//!                     }
//!                     MessageRecord::Connection(conn) => {
//!                         // ..
//!                         # drop(conn);
//!                     }
//!                 }
//!             }
//!         },
//!         ChunkRecord::IndexData(index_data) => {
//!             // ..
//!             # drop(index_data);
//!         },
//!     }
//! }
//! // Iterate over records in the index section
//! for record in bag.index_records() {
//!     match record? {
//!         IndexRecord::IndexData(index_data) => {
//!             // ..
//!             # drop(index_data);
//!         }
//!         IndexRecord::Connection(conn) => {
//!             // ..
//!             # drop(conn);
//!         }
//!         IndexRecord::ChunkInfo(chunk_info) => {
//!             // ..
//!             # drop(chunk_info);
//!         }
//!     }
//! }
//! # Ok(()) }
//! ```
#![doc(html_root_url = "https://docs.rs/rosbag/0.5.0")]
#![warn(missing_docs, rust_2018_idioms)]

use memmap2::Mmap;
use std::{fs, io, path::Path, result, str, sync::Arc};

const VERSION_STRING: &str = "#ROSBAG V2.0\n";
const VERSION_LEN: u64 = VERSION_STRING.len() as u64;
const ROSBAG_HEADER_OP: u8 = 0x03;

mod cursor;
mod error;
mod field_iter;
mod record;

mod chunk_iter;
mod index_iter;
mod msg_iter;
pub mod record_types;

use cursor::Cursor;
use field_iter::FieldIterator;
use record_types::utils::{check_op, set_field_u32, set_field_u64};

pub use chunk_iter::{ChunkRecord, ChunkRecordsIterator};
pub use error::Error;
pub use index_iter::{IndexRecord, IndexRecordsIterator};
pub use msg_iter::{MessageRecord, MessageRecordsIterator};

/// Open rosbag file.
pub struct RosBag {
    file: Arc<fs::File>,
    data: Mmap,
    start_pos: usize,
    index_pos: usize,
    conn_count: u32,
    chunk_count: u32,
}

/// A specialized Result type for ROS bag file reading and parsing.
pub type Result<T> = result::Result<T, Error>;

/// Bag file header record which contains basic information about the file.
#[derive(Debug, Clone)]
struct BagHeader {
    /// Offset of first record after the chunk section
    index_pos: u64,
    /// Number of unique connections in the file
    conn_count: u32,
    /// Number of chunk records in the file
    chunk_count: u32,
}

fn parse_bag_header(data: &[u8]) -> Result<(u64, BagHeader)> {
    let mut cursor = Cursor::new(data);

    if cursor.next_bytes(VERSION_LEN)? != VERSION_STRING.as_bytes() {
        return Err(Error::InvalidHeader);
    }

    let header = cursor.next_chunk()?;

    let mut index_pos: Option<u64> = None;
    let mut conn_count: Option<u32> = None;
    let mut chunk_count: Option<u32> = None;
    let mut op: bool = false;

    for item in FieldIterator::new(header) {
        let (name, val) = item?;
        match name {
            "op" => {
                check_op(val, ROSBAG_HEADER_OP)?;
                op = true;
            }
            "index_pos" => set_field_u64(&mut index_pos, val)?,
            "conn_count" => set_field_u32(&mut conn_count, val)?,
            "chunk_count" => set_field_u32(&mut chunk_count, val)?,
            _ => log::warn!("unexpected field in bag header: {}", name),
        }
    }

    let bag_header = match (index_pos, conn_count, chunk_count, op) {
        (Some(index_pos), Some(conn_count), Some(chunk_count), true) => BagHeader {
            index_pos,
            conn_count,
            chunk_count,
        },
        _ => return Err(Error::InvalidHeader),
    };

    // jump over header data
    let _ = cursor.next_chunk()?;

    Ok((cursor.pos(), bag_header))
}

impl RosBag {
    /// Create a new iterator over provided path to ROS bag file.
    pub fn new<P: AsRef<Path>>(path: P) -> io::Result<Self> {
        let file = Arc::new(fs::File::open(path)?);
        advise_sequential_access(file.as_ref());
        let data = unsafe { Mmap::map(&file)? };

        let (start_pos, header) = parse_bag_header(&data).map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "Invalid or unsupported rosbag header",
            )
        })?;

        Ok(Self {
            file,
            data,
            start_pos: start_pos.try_into().unwrap(),
            conn_count: header.conn_count,
            index_pos: header.index_pos.try_into().unwrap(),
            chunk_count: header.chunk_count,
        })
    }

    /// Get connection count in this rosbag file.
    pub fn get_conn_count(&self) -> u32 {
        self.conn_count
    }

    /// Get chunk count in this rosbag file.
    pub fn get_chunk_count(&self) -> u32 {
        self.chunk_count
    }

    /// Get iterator over records in the chunk section.
    pub fn chunk_records(&self) -> ChunkRecordsIterator<'_> {
        let cursor = Cursor::new(&self.data[self.start_pos..self.index_pos]);
        ChunkRecordsIterator {
            cursor,
            offset: self.start_pos as u64,
            file: self.file.clone(),
        }
    }

    /// Get iterator over records in the index section.
    pub fn index_records(&self) -> IndexRecordsIterator<'_> {
        let cursor = Cursor::new(&self.data[self.index_pos..]);
        IndexRecordsIterator {
            cursor,
            offset: self.index_pos as u64,
        }
    }
}

fn advise_sequential_access(file: &fs::File) {
    #[cfg(target_family = "unix")]
    {
        use std::os::unix::io::AsRawFd;

        let ret =
            unsafe { libc::posix_fadvise(file.as_raw_fd(), 0, 0, libc::POSIX_FADV_SEQUENTIAL) };
        if ret != 0 {
            log::warn!(
                "posix_fadvise(POSIX_FADV_SEQUENTIAL) failed: {}",
                io::Error::from_raw_os_error(ret)
            );
        }
    }

    #[cfg(not(target_family = "unix"))]
    let _ = file;
}
